"""The HQPlayer output against a fake control port: the queue mirrored in the
URI form each file-access mode dictates, a running playlist adopted back into
queue items, drift read through HQPlayer's escapes, and the re-mirror after
HQPlayer restarts (a trial-mode Embedded stops every 30 minutes)."""

import errno
import itertools
import logging
import os
import re
import socket
import socketserver
import sys
import threading
import time
import traceback
import types
from contextlib import contextmanager
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
        # Parts of URIs whose PlaylistAdd it refuses — files it cannot open
        self.refuse: set = set()
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
        # The epoch each connection was accepted in, until its handler runs
        self._accepted: dict = {}
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
                with fake._lock:
                    epoch = fake._accepted.pop(self.request)
                if fake.mute or fake._down:
                    return              # a trial-stopped Embedded, or a box going down: dropped at once
                with fake._lock:
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

        class Server(_ReusableServer):
            # A connection is taken under the fake's lock as it is accepted,
            # so a restart finds every one the box had — served, waiting for
            # its handler, or still in the kernel's queue — and resets them
            # all, as a real restart does

            def server_activate(self):
                super().server_activate()
                self.socket.setblocking(False)  # a restart may empty the queue first

            def get_request(self):
                with fake._lock:
                    request, address = self.socket.accept()
                    request.setblocking(True)
                    fake._conns.append(request)
                    fake._accepted[request] = fake._epoch
                return request, address

        self._handler, self._server = Handler, Server
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
        self._srv = self._server(("127.0.0.1", self.port), self._handler)
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
            if cmd == "PlaylistAdd" and any(part in attrs["uri"] for part in self.refuse):
                return '<PlaylistAdd result="Error">Failed to open the file</PlaylistAdd>'
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
            while True:
                try:
                    c, _ = self._srv.socket.accept()
                except OSError:
                    break           # none queued, or the port is closed
                conns.append(c)
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
    # ...and what GetInfo said goes on the endpoint row, a benchmark the
    # process died in is put back, and one left to the owner is said — all
    # in the database.
    from playback import hqp_benchmark
    monkeypatch.setattr(hqp_library, "note_info", lambda *a, **k: None)
    monkeypatch.setattr(hqp_benchmark, "recover", lambda endpoint_id, link, **kw: None)
    monkeypatch.setattr(hqp_benchmark, "left_pending", lambda endpoint_id: False)
    # ...and its endpoint row: this HQPlayer's library is endpoint 7 here.
    monkeypatch.setattr(hqp_library, "endpoint_by_address", lambda host, port: {"id": 7, "name": "fake"})
    # The output switch re-reads every slot's copies from the database
    # (playback.substitute); here every item opens as queued.
    from playback import substitute
    monkeypatch.setattr(substitute, "native_plays",
                        lambda items, output_id, endpoint_id: [None] * len(items))
    # The playback trace is process-wide — the hand-over ledger, the attempts,
    # the failing run, a refusal's notice, the client's failures: each test
    # starts and leaves them empty, whatever order the tests run in.
    _fresh_trace()
    yield f
    _fresh_links()
    f.close()
    _fresh_trace()


def _fresh_trace() -> None:
    with diag._lock:
        diag._ledger.clear()
        diag._by_slot.clear()
        diag._ring.clear()
        diag._run_key = None
        diag._refused = None
    with HQPlayerClient._error_lock:
        HQPlayerClient._error_ring.clear()


def test_a_queue_handed_to_an_hqplayer_that_does_not_answer_gives_up_at_once(fake, monkeypatch,
                                                                              caplog):
    monkeypatch.setattr(hl, "COMMAND_WAIT_S", 0.3)
    fake.silence(30)
    t = time.monotonic()
    link = _link()
    with link.command():
        taken, unsent = hb._add_uris_with_retry(
            link, [f"http://127.0.0.1:1/file/t{i}" for i in range(6)], clear_first=True)
    # one track's two tries, not six tracks' — and in what it is, not a refusal
    assert not any(taken) and unsent == 0 and time.monotonic() - t < 3
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


def test_a_tap_after_an_attach_that_found_another_controller_playing_takes_the_queue(
        fake, monkeypatch):
    """HQPlayer plays another controller's playlist when it is attached: the
    attach leaves it playing and judges it at once — a tap before the
    poller's first answer hands HQPlayer the queue and plays the tapped
    track, never selects into the other controller's playlist."""
    from playback import manager as manager_mod
    monkeypatch.setattr(HqpBackend, "_poll_loop", lambda self: None)    # no first answer
    fake.playlist = ["file:///Elsewhere/theirs-1.flac", "file:///Elsewhere/theirs-2.flac"]
    fake.state, fake.track = int(PlaybackState.PLAYING), 1
    mgr = _album(3)
    monkeypatch.setattr(manager_mod, "manager", mgr)
    b = _attach(mgr)
    try:
        assert fake.playlist[0] == "file:///Elsewhere/theirs-1.flac"     # left playing
        assert b.select(2)
        assert _wait(lambda: fake.playlist == [f"file:///E:/Music/A/0{i}.flac" for i in (1, 2, 3)]
                     and fake.state == int(PlaybackState.PLAYING) and fake.track == 2, timeout=10)
    finally:
        b.shutdown()


def test_drift_is_read_through_hqplayers_escapes(fake):
    mgr = PlaybackManager()
    mgr.queue.replace([_item("E:/Music/A [TR24]/01.flac", 1)])
    b = HqpBackend(emit=mgr._on_backend_status, queue=mgr.queue)
    stopped = TrackStatus(state=PlaybackState.STOPPED, track_index=0, track_id="", position=0.0,
                          length=0.0, volume=-3.0)
    b._judge_mirror([{"uri": "file:///E:/Music/A%20%5BTR24%5D/01.flac"}], stopped,
                    expect=b._mirror_moves)
    assert not b.drift
    b._judge_mirror([{"uri": "file:///E:/Music/A%20%5BTR24%5D/02.flac"}], stopped,
                    expect=b._mirror_moves)
    assert b.drift
    b._in_step()
    b._judge_mirror([], stopped, expect=b._mirror_moves)
    assert b.drift


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
    b._mirror = "drift"
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
    b._mirror = "drift"
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
    b._mirror = "drift"
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
        # The entry said it; the canary, asked at that same answer (an entry
        # not ours read while in step), finds the playlist drifted too
        assert _wait(lambda: b.drift)
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


def test_a_restart_loses_the_mirror_and_hqplayer_gets_the_queue_back(fake, monkeypatch):
    """A restart drops the mirror (a trial Embedded stops every 30 minutes):
    HQPlayer, back and stopped, takes the queue afresh at its first answer —
    the play press finds it there, with no re-attach."""
    from playback import manager as manager_mod
    mgr = PlaybackManager()
    monkeypatch.setattr(manager_mod, "manager", mgr)
    mgr.queue.replace([_item("E:/Music/A/01.flac", 1), _item("E:/Music/A/02.flac", 2)])
    b = _attach(mgr)
    try:
        assert len(fake.playlist) == 2
        assert b.healthy() and b.reachable()
        fake.restart()
        assert _wait(lambda: b.mirror_losses >= 1, timeout=12.0), "the return never lost the mirror"
        assert _wait(lambda: fake.playlist == ["file:///E:/Music/A/01.flac",
                                               "file:///E:/Music/A/02.flac"], timeout=10.0)
        assert mgr.active is b and b.healthy()
    finally:
        b.shutdown()


def test_a_return_after_a_long_outage_also_loses_the_mirror(fake, monkeypatch):
    """The Pi was power-cycled for half a minute (live, 2026-09-27): the
    failed polls had already reset the status client, so a same-tick socket
    comparison saw no reconnect, the empty playlist passed as an external
    edit and Play landed on nothing."""
    # The breaker's wall clock is not under test: a failed tick between the
    # return and a hand reset re-armed it, and the back-off then outlasted
    # the wait (the test failed about one run in eight).
    monkeypatch.setattr(hl, "CIRCUIT_COOLDOWN_S", 0.0)
    from playback import manager as manager_mod
    mgr = PlaybackManager()
    monkeypatch.setattr(manager_mod, "manager", mgr)
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
        assert _wait(lambda: (b.poke(), b.mirror_losses >= 1)[1], timeout=15.0), \
            "the return never lost the mirror"
        assert _wait(lambda: fake.playlist == ["file:///E:/Music/A/01.flac",
                                               "file:///E:/Music/A/02.flac"], timeout=10.0)
        assert mgr.active is b and b.healthy()
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
        assert b._first_divergence(playlist, mgr.queue.snapshot()) is None
        fake.playlist[1] = "file:///media/FLASH/Other/01.flac"
        with _link().asking() as c:
            playlist = c.get_playlist()
        assert b._first_divergence(playlist, mgr.queue.snapshot()) == 2
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
    snapshot = mgr.queue.snapshot()
    assert b.queue_replace(snapshot, play=False) == snapshot
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


def test_a_rebind_hqplayer_cannot_follow_loses_the_mirror_until_it_answers(fake):
    mgr = _album(2)
    b = _mirrored(mgr)
    fake.close()
    _move(mgr, 1, "E:/Music/B/01.flac", 10)
    b.queue_changed("rebind")
    # HQPlayer gets the queue back once it answers again (_reconverge); the
    # output is no less healthy for it — a switch away still owes its Stop
    assert b._mirror == "lost" and b._reconverge_due and b.healthy()


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
                        lambda endpoint_id, link, **kw: calls.append(endpoint_id))
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
                        lambda endpoint_id, link, **kw: calls.append(endpoint_id))
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


def test_a_refusal_before_the_silence_that_stops_the_batch_is_told_apart_from_it(monkeypatch):
    # "a" refused (the file cannot be opened) — final, its connection kept,
    # no retry; "b"'s add met HQPlayer's silence and stops the batch at once —
    # no playlist check, no retry, each would wait out the same silence.
    class Stub:
        timed_out = answered = False

        def playlist_add(self, uri, clear=False):
            self.timed_out = uri == "b"
            self.answered = uri != "b"
            return uri not in ("a", "b")
    link = types.SimpleNamespace(client=Stub(), attached=None)
    monkeypatch.setattr(hb, "_refusal", lambda hqp: ("Error", "cannot open it"))
    checked = []
    monkeypatch.setattr(hb, "_uri_in_playlist",
                        lambda hqp, uri: checked.append(uri) or uri == "b")
    monkeypatch.setattr(hb, "_stream_mode", lambda: False)
    # taken: "c"; refused: "a" (before the silence); not handed over: "b" on
    assert hb._add_uris_with_retry(link, ["a", "c", "b"]) == ([False, True, False], 2)
    assert checked == []


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
        b._mirror = "drift"
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
        nine = _item("E:/Music/B/01.flac", 9)
        assert b.queue_insert_next([nine], 1) == ([nine], 1)
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
                           new_before=[], old_after=items[1:], new_after=[items[2], items[1]],
                           order=[items[0], items[2], items[1]], items=items, output=b)
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
    plan = ReorderPlan(seamless=False, status_idx=2, new_status_idx=1, old_before=[items[0]],
                       new_before=[], old_after=[items[2]], new_after=[items[0], items[2]],
                       order=[items[1], items[0], items[2]], position=0, resume=False)
    assert b.queue_reorder(plan)["interrupted"]
    mgr.queue.reorder(plan.order)      # the manager's commit
    assert fake.playlist == ["file:///E:/Music/A/02.flac", "file:///E:/Music/A/01.flac",
                             "file:///E:/Music/A/03.flac"]
    assert "SelectTrack" not in [c for c, _ in fake.commands]
    assert fake.state == int(PlaybackState.STOPPED) and b._spot_at() == (1, 0)
    fake.commands.clear()
    assert b.play()
    assert [(c, a) for c, a in fake.commands if c in ("SelectTrack", "Play")][:2] == [
        ("SelectTrack", {"index": "1"}), ("Play", {})]
    assert (fake.state, fake.track) == (int(PlaybackState.PLAYING), 1)


# -- a DSP change waits out a build (hqp_link.BuildWatch, change_dsp) --------------------
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

    def started():
        return [(c, a) for c, a in fake.commands if c in ("SelectTrack", "Seek", "Play")]
    assert _wait(lambda: len(started()) >= 3)       # read once the resume sent its Play
    start = started()
    assert start[0] == ("SelectTrack", {"index": "1"})
    assert start[1][0] == "Seek" and int(start[1][1]["position"]) >= 42
    assert start[2] == ("Play", {})
    assert _wait(lambda: mgr._latest_status["state"] == "playing")
    assert _link().build.last_change["applied"] == {"filter": 57, "filter1x": 57}


def test_a_change_over_another_controllers_entry_resumes_nothing_of_ours(fake, output):
    """HQPlayer plays an entry that is not ours (slot 0 on the status) when a
    change stops it for good: nothing of the queue is started at that
    entry's second."""
    mgr, b = output
    with fake._lock:
        fake.playlist.append("file:///Elsewhere/theirs.flac")
        fake.track, fake.position = len(fake.playlist), 30.0
    assert _wait(lambda: mgr._latest_status.get("source") == "external")
    fake.build("SetFilter", 1.0, answer="last", stops=True)
    hb.change_dsp({"filter": 57, "filter1x": 57})
    assert _wait(lambda: _link().build.last_change is not None, timeout=5)
    time.sleep(0.5)                                  # what a resume would send
    assert not [c for c, _ in fake.commands if c in ("SelectTrack", "Seek", "Play")]


def test_a_change_on_a_playlist_that_is_no_mirror_resumes_the_queues_track(fake, output):
    """The mirror lost, HQPlayer holding an entry of its own before ours: the
    resume after a change that stopped it is a play press — the queue taken
    afresh and its track started — never the queue's slot sent as
    HQPlayer's index."""
    mgr, b = output
    with fake._lock:
        fake.playlist.insert(0, "file:///Elsewhere/theirs.flac")
        fake.track, fake.position = 3, 60.0             # entry 3 = the queue's slot 2
    assert _wait(lambda: mgr._latest_status.get("track_index") == 2)
    b.lose_mirror("HQPlayer took 0 of 1 appended tracks", by="refusal")
    fake.build("SetFilter", 1.0, answer="last", stops=True)
    hb.change_dsp({"filter": 57, "filter1x": 57})
    assert _wait(lambda: fake.playlist == [f"file:///E:/Music/A/0{i}.flac" for i in (1, 2, 3)]
                 and fake.state == int(PlaybackState.PLAYING) and fake.track == 2, timeout=10)
    assert _wait(lambda: mgr._latest_status.get("track_id") == "track-2")


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


def test_a_dsp_change_while_the_output_is_lent_is_busy_not_unreachable(fake, output, monkeypatch):
    """The HQPlayer screen's change while the benchmark has the output: 409
    naming the job — never 503 "not reachable" — and nothing sent."""
    from fastapi import HTTPException
    from routers import hqplayer as hqp_router
    mgr, b = output
    _picker(monkeypatch, fake)
    monkeypatch.setattr(hqp_router, "manager", mgr)
    try:
        with mgr.hold("benchmark", "The HQPlayer benchmark is running"):
            n = len(fake.commands)
            with pytest.raises(HTTPException) as e:
                hqp_router.set_config(hqp_router.ConfigRequest(shaper=2))
            assert fake.commands[n:] == []
        assert e.value.status_code == 409 and "benchmark is running" in e.value.detail
    finally:
        if mgr.active is not None and mgr.active is not b:
            mgr.active.shutdown()


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
    plan = ReorderPlan(seamless=False, status_idx=2, new_status_idx=1, old_before=[items[0]],
                       new_before=[], old_after=[items[2]], new_after=[items[0], items[2]],
                       order=[items[1], items[0], items[2]], position=150, resume=False)
    b.queue_reorder(plan)
    mgr.queue.reorder(plan.order)      # the manager's commit
    assert fake.state == int(PlaybackState.STOPPED)
    assert not [c for c, _ in fake.commands if c in ("SelectTrack", "Seek", "Play")]   # no sound
    stopped = TrackStatus(state=PlaybackState.STOPPED, track_index=0, track_id="",
                          position=0.0, length=0.0, volume=-3.0)
    assert b._status_of(stopped, (0, None, False)).extra["resume"] == {"index": 1, "position": 150}
    fake.commands.clear()
    assert b.play()
    assert [(c, a) for c, a in fake.commands if c in ("SelectTrack", "Seek", "Play")] == [
        ("SelectTrack", {"index": "1"}), ("Seek", {"position": "150"}), ("Play", {})]


def test_a_start_a_pause_dropped_leaves_its_spot_for_the_play_press(fake, output):
    """The start a rebuild left, dropped by a Pause before it went,
    leaves the slot and second it was for as the spot — the next Play
    resumes them, never HQPlayer's first entry from 0 s."""
    from playback.base import ReorderPlan
    mgr, b = output
    with fake._lock:
        fake.track, fake.position = 2, 120.0
    assert _wait(lambda: mgr._latest_status.get("track_index") == 2)
    items = mgr.queue.snapshot()
    plan = ReorderPlan(seamless=False, status_idx=2, new_status_idx=1, old_before=[items[0]],
                       new_before=[], old_after=[items[2]], new_after=[items[0], items[2]],
                       order=[items[1], items[0], items[2]], position=120, resume=True)
    b.queue_reorder(plan)
    mgr.queue.reorder(plan.order)      # the manager's commit
    b.pause()                                       # before the start the rebuild left goes
    n = len(fake.commands)
    b.queue_start()
    assert "SelectTrack" not in [c for c, _ in fake.commands[n:]]
    assert b._spot_at() == (1, 120)
    fake.commands.clear()
    assert b.play()
    assert [(c, a) for c, a in fake.commands if c in ("SelectTrack", "Seek", "Play")] == [
        ("SelectTrack", {"index": "1"}), ("Seek", {"position": "120"}), ("Play", {})]


def test_a_start_a_newer_intent_dropped_is_interrupted_never_unreachable(fake, output):
    """A step the link did not send — a Pause came first — closes its attempt
    as superseded: interrupted, never "HQPlayer did not answer", and a run of
    them is no HQPlayer failing."""
    from playback.base import ReorderPlan
    mgr, b = output
    for _ in range(3):
        items = mgr.queue.snapshot()
        plan = ReorderPlan(seamless=False, status_idx=1, new_status_idx=2,
                           old_before=[], new_before=[items[1]], old_after=items[1:],
                           new_after=[items[2]], order=[items[1], items[0], items[2]],
                           position=10, resume=True)
        b.queue_reorder(plan)
        att = b._attempt
        mgr.queue.reorder(plan.order)
        b.pause()
        b.queue_start()
        assert _wait(lambda: att.verdict is not None, timeout=5)
        assert att.verdict["code"] == "interrupted", att.verdict
    assert diag.failing_run() is None


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


# -- background work meets a build (hqp_link.through); the spot; the takeover -------------
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
    assert _wait(lambda: b.mirror_losses >= 1, timeout=12.0), "the restart never lost the mirror"
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
                                old_before=[items[0]], new_before=[items[2]],
                                old_after=[items[2]], new_after=[items[0]],
                                order=[items[2], items[1], items[0]],
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
    # The same HQPlayer stays the output as it is — no detach, no attach
    assert mgr.active is b and _link().build.silent() is not None
    assert _wait(lambda: _link().build.state is None)
    assert fake.sent_into_silence() == []
    assert fake.state == int(PlaybackState.PLAYING) and len(fake.playlist) == 3


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


@pytest.mark.parametrize("bad", [{"stream_quality": "bogus"}, {"port": 70000}])
def test_an_output_change_with_a_bad_field_changes_nothing(fake, output, monkeypatch, bad):
    """Every field is checked before anything is chosen or written: a 400
    leaves the HQPlayer driven, its link and the settings as they were."""
    from fastapi import HTTPException
    mgr, b = output
    settings_router = _picker(monkeypatch, fake)
    before = (hl.selected(), settings.hqplayer_host, settings.hqplayer_port,
              settings_router._read("hqplayer.host"))
    with pytest.raises(HTTPException) as e:
        _pick(settings_router, type="hqplayer",
              hqplayer={"host": "127.0.0.2", "port": bad.get("port", fake.port)},
              stream_quality=bad.get("stream_quality"))
    assert e.value.status_code == 400
    assert (hl.selected(), settings.hqplayer_host, settings.hqplayer_port,
            settings_router._read("hqplayer.host")) == before
    assert mgr.active is b and b._link.state == "open"


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
    # The switch returns once the Stop went, or was found not needed: its
    # arrival is HQPlayer's to log
    if stopped:
        assert _wait(lambda: "Stop" in [c for _, c in fake.arrivals[n:]], timeout=3)
    else:
        assert "Stop" not in [c for _, c in fake.arrivals[n:]]
    assert b is not mgr.active


class _QuietLink:
    """What a build watch asks of its link, and nothing more."""
    state, why = "open", None
    _local = threading.local()

    def _not_answering(self, command, waited):
        pass

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
    assert b._mirror == "lost"
    fake.come_back()
    want = [f"file:///E:/Music/{d}/0{i}.flac" for d, i in
            (("A", 1), ("A", 2), ("A", 3), ("B", 1), ("B", 2), ("B", 3))]
    assert _wait(lambda: (b.poke(), fake.playlist == want)[1], timeout=20)
    assert _wait(lambda: b._mirror == "step", timeout=10)


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


def test_the_node_stop_ends_a_filler_at_once(fake, output, monkeypatch):
    """A filler waiting out a build when the node stops ends there —
    no add tried again, no seconds slept per track."""
    mgr, b = output
    player_router = _routes_on(monkeypatch, mgr)
    waiting = threading.Event()
    wait_out = hl.BuildWatch.wait_out
    monkeypatch.setattr(hl.BuildWatch, "wait_out",
                        lambda self, until: (waiting.set(), wait_out(self, until))[1])
    fake.build("SetFilter", 30.0)
    hb.change_dsp({"filter": 57, "filter1x": 57})
    album = [_item(f"E:/Music/B/0{i}.flac", 10 + i) for i in range(1, 4)]
    done = threading.Event()
    t = threading.Thread(target=lambda: (player_router._owned_filler(album, mgr.queue.generation),
                                         done.set()), daemon=True)
    t.start()
    assert waiting.wait(5)
    t0 = time.monotonic()
    hl.close_all("the node is stopping")
    assert done.wait(2.0), "the filler went on after the node stop"
    assert time.monotonic() - t0 < 1.0
    assert len(mgr.queue) == 3


def test_a_file_the_output_refused_is_offered_once(fake, output, monkeypatch):
    """A filler's file HQPlayer refuses is not tried again — one add,
    no seconds slept, the track left out."""
    mgr, b = output
    player_router = _routes_on(monkeypatch, mgr)
    fake.refuse.add("B/09.flac")
    n = len(fake.commands)
    t0 = time.monotonic()
    assert player_router._filler_append(_item("E:/Music/B/09.flac", 9), mgr.queue.generation) is False
    assert time.monotonic() - t0 < 1.0
    assert [a["uri"] for c, a in fake.commands[n:] if c == "PlaylistAdd"] == [
        "file:///E:/Music/B/09.flac"]
    assert len(mgr.queue) == 3


def test_an_add_the_link_let_go_lands_once_the_output_change_is_over(fake, output, monkeypatch):
    """An add the link did not send because the output changed under it
    goes again once the change is over — on the output there is then, and
    with no sleep in between."""
    mgr, b = output
    player_router = _routes_on(monkeypatch, mgr)
    _away(monkeypatch, fake)
    append, tried = mgr.append, threading.Event()

    def let_go_once(*args, **kwargs):
        if not tried.is_set():
            tried.set()
            raise hl.Dropped("another HQPlayer was chosen")
        return append(*args, **kwargs)
    monkeypatch.setattr(mgr, "append", let_go_once)
    placed = {}
    t = threading.Thread(target=lambda: placed.setdefault("it", player_router._filler_append(
        _item("E:/Music/B/09.flac", 9), mgr.queue.generation)), daemon=True)
    t.start()
    assert tried.wait(5)
    time.sleep(0.3)                                  # it waits: nothing to land on yet
    assert "it" not in placed
    t0 = time.monotonic()
    mgr.activate(None)
    mgr.activate("browser")
    t.join(5)
    assert placed.get("it") is True and time.monotonic() - t0 < 1.0
    assert [it.track_id for it in mgr.queue.snapshot()][-1] == "track-9"


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


@pytest.mark.parametrize("case", ["database", "busy", "clear refused", "taken"])
def test_the_radio_rotates_history_under_the_command_hold_before_the_clear(fake, output,
                                                                         monkeypatch, case):
    """The radio start: the rotation runs under HQPlayer's command hold,
    before PlaylistClear — a rotation that fails clears nothing; a hold
    refused (HQPlayer building) rotates nothing; a clear HQPlayer does not
    take leaves the queue as it is, history rotated."""
    from fastapi import HTTPException
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
    if case == "busy":
        fake.build("SetFilter", 3.0, answer="last")
        threading.Thread(target=lambda: hb.change_dsp({"filter": 57, "filter1x": 57}),
                         daemon=True).start()
        assert _wait(lambda: _link().build.silent() is not None, timeout=3)
    fake.commands.clear()
    req = player_router.RadioStartRequest(track_uuid="0b8f1a2e-1d2c-5e3f-9a4b-5c6d7e8f9a0b")
    if case in ("database", "busy"):
        with pytest.raises(RuntimeError if case == "database" else HTTPException):
            player_router.radio_start(req)
        assert "PlaylistClear" not in [c for c, _ in fake.commands]
        assert len(mgr.queue) == 3
    else:
        player_router.radio_start(req)
        assert "PlaylistClear" in [c for c, _ in fake.commands]
        assert len(mgr.queue) == (3 if case == "clear refused" else 1)
    # rotated once, before the clear — never when the hold was refused
    assert order == ([] if case == "busy" else [False])


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


@pytest.mark.parametrize("how", ["unfinished", "not answering"])
def test_the_assistants_gates_quote_the_links_own_line(fake, output, monkeypatch, how):
    """One line for an HQPlayer that refuses people — the link's — on the
    output settings, in the MCP gate and in the in-process one: a build it
    did not finish says restart HQPlayer, a silence past a Stop's wait says
    it is not answering."""
    import importlib.util
    from tools import definitions
    mgr, b = output
    settings_router = _picker(monkeypatch, fake)
    spec = importlib.util.spec_from_file_location(
        "assistant_server_gate", BACKEND.parent / "mcp" / "assistant_server.py")
    server = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(server)
    monkeypatch.setattr(server, "_output_prefs", settings_router.get_output_prefs)
    if how == "unfinished":
        _limit(monkeypatch, 0.6)
        fake.build("SetFilter", 3.0)
        hb.change_dsp({"filter": 57, "filter1x": 57})
        assert _wait(lambda: (_link().build.silent() or {}).get("unfinished"), timeout=5)
        line = hl.UNFINISHED
    else:
        monkeypatch.setattr(hl, "STOP_ANSWER_S", 0.6)
        _link().client.waits.update(hl._command_waits())
        fake.build("Stop", 3.0, answer="last")
        assert not b.stop()
        assert _wait(lambda: _link().silent_since is not None, timeout=3)
        line = hl.NOT_ANSWERING
    assert settings_router.get_output_prefs()["busy"] == line
    with pytest.raises(ConnectionError, match=re.escape(line)):
        server._hqp_gate()
    with pytest.raises(ConnectionError, match=re.escape(line)):
        definitions._hqp_gate()


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
            seen["between"] = asked.wait(2)          # the question went meanwhile
            seen["why"] = link.client.refusal()
    def question():
        with link.asking() as c:
            seen["answered"] = c.get_status() is not None
        asked.set()
    t = threading.Thread(target=command, daemon=True)
    t.start()
    assert sent.wait(5)
    q = threading.Thread(target=question, daemon=True)  # its turn may never come
    q.start()
    t.join(5)
    q.join(5)
    assert seen == {"ok": False, "between": True, "why": "Nothing to pause", "answered": True}


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
    and the build's silence falls on it, watched, never on the poller; the
    knob still to go is not sent."""
    mgr, b = output
    _quick_polls(monkeypatch)
    link = _link()
    quiet = _seen(mgr, "not_answering")
    fake.build("SetFilter", 2.0)                         # answered, then silent

    def superseding(f, conn, what, command):
        if what == "out" and command == "SetFilter":
            link.accept("dsp")                           # the rest of this change goes
    fake.on_wire = superseding
    out = hb.change_dsp({"filter": 57, "filter1x": 57, "shaper": 2})
    assert out["failed"]["shaper"] == "not sent: a newer DSP change came first", out
    assert _wait(lambda: link.build.state is not None, timeout=3)
    after = [c for _, c in fake.arrivals]
    assert after[after.index("SetFilter") + 1] == "Status"
    assert _wait(lambda: link.build.state is None, timeout=10)
    assert "SetShaping" not in [c for _, c in fake.arrivals]
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
    assert _wait(lambda: fake.silences, timeout=3)       # inside the teardown
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
    fake.service["GetInfo"] = 2.0
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
    assert _wait(lambda: len(link._waiting) == 1, timeout=3)   # its turn behind the GetInfo
    lending = threading.Thread(target=link.lend, daemon=True)
    lending.start()
    # dropped while it waits — before the GetInfo it waits behind is answered
    assert _wait(lambda: "second" in results, timeout=1.5) and "first" not in results
    first.join(5)
    second.join(5)
    lending.join(5)
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
    waits = threading.Event()
    wait = link.build._changed.wait

    def watched(timeout=None):
        waits.set()
        return wait(timeout)
    monkeypatch.setattr(link.build._changed, "wait", watched)

    def waiting():
        gave_up["why"] = link.build.wait_out()
    t = threading.Thread(target=waiting, daemon=True)
    t.start()
    assert waits.wait(3)                                 # inside wait_out's wait
    t0 = time.monotonic()
    hl.close_all("the node is stopping")
    t.join(5)
    assert time.monotonic() - t0 < 2
    assert isinstance(gave_up["why"], hl.GaveUp) and "the node is stopping" in str(gave_up["why"])


# -- The mirror and the switch-away Stop (2026-10-12) --------------------------------
# One state for what HQPlayer's playlist is to the queue (in step, a tail
# mirror, lost, drifted); what HQPlayer refuses leaves the queue only when it
# took the rest of the batch; a play press on a playlist that is no mirror
# rebuilds it; the Stop a switch away owes is judged on what HQPlayer was
# given, and waits for an HQPlayer that is silent.


class _Away:
    """The output switched to: it takes every command and plays nothing."""
    id, label = "browser", "elsewhere"

    def __init__(self, emit, queue):
        pass

    def start(self):
        pass

    def shutdown(self):
        pass

    def release(self):
        return None

    def healthy(self):
        return True

    def reachable(self):
        return True

    def resume_at(self, index):
        pass

    def queue_replace(self, items, *, play, probe_first=False):
        return list(items)

    def queue_append(self, items):
        return list(items)

    def queue_insert_next(self, items, anchor_index):
        return list(items), anchor_index

    def queue_changed(self, kind, *, play=False):
        pass

    def queue_start(self):
        pass

    def queue_abandon(self):
        pass

    @contextmanager
    def mutation(self):
        yield

    def lose_mirror(self, why):
        pass


def _away(monkeypatch, fake):
    """The output can be switched to `_Away`, its settings kept in memory."""
    from playback import browser_backend
    monkeypatch.setattr(browser_backend, "BrowserBackend", _Away)
    _picker(monkeypatch, fake)


def _stopped(mgr, fake):
    with fake._lock:
        fake.state = int(PlaybackState.STOPPED)
    assert _wait(lambda: mgr._latest_status.get("state") == "stopped")


def test_one_refused_file_with_the_mirror_lost_costs_one_handover_not_a_loop(fake, output,
                                                                             monkeypatch):
    """HQPlayer back and stopped with one file of the queue it cannot open:
    it gets the queue once — the refused file a gap of it, in the queue
    still and named — never an endless round of Stop, clear and re-adds."""
    mgr, b = output
    monkeypatch.setattr(hl, "CIRCUIT_COOLDOWN_S", 0.0)
    _stopped(mgr, fake)
    fake.refuse.add("A/02.flac")
    n = len(fake.commands)
    fake.restart()                                   # back empty and stopped
    time.sleep(4.0)                                  # what a loop would send meanwhile
    adds = [c for c, _ in fake.commands[n:] if c == "PlaylistAdd"]
    assert len(adds) == 3, len(adds)                 # one hand-over, the refused file once
    assert [it.track_id for it in mgr.queue.snapshot()] == ["track-1", "track-2", "track-3"]
    assert fake.playlist == ["file:///E:/Music/A/01.flac", "file:///E:/Music/A/03.flac"]
    assert [t.get("gap") for t in mgr.playlist()["tracks"]] == [None, hb.REFUSED_HERE, None]


def test_a_question_that_times_out_is_no_return_of_hqplayer(fake, output):
    """A reconnect of our own after a question went unanswered is no
    return of HQPlayer — an edit made in its own window stays as it was,
    never overwritten by a re-mirror."""
    mgr, b = output
    _stopped(mgr, fake)
    with fake._lock:
        fake.playlist.append("file:///Elsewhere/theirs.flac")    # an edit in HQPlayer's window
    n = len(fake.commands)
    fake.silence(1.0)
    with _link().asking() as c:
        assert c.get_info(wait=0.3) is None                       # unanswered: the socket goes
    time.sleep(3.0)
    assert fake.playlist[-1] == "file:///Elsewhere/theirs.flac"
    assert not [a for c, a in fake.commands[n:] if c == "PlaylistAdd"]


def test_a_lost_mirror_still_owes_the_switch_away_stop(fake, output, monkeypatch):
    """A mirror lost while HQPlayer plays on — an add it did not answer
    — is no health matter: the switch away still stops it."""
    mgr, b = output
    _away(monkeypatch, fake)
    b.lose_mirror("HQPlayer took 0 of 1 appended tracks")
    n = len(fake.arrivals)
    mgr.activate(None)
    mgr.activate("browser")
    assert _wait(lambda: "Stop" in [c for _, c in fake.arrivals[n:]], timeout=5)
    assert fake.state == int(PlaybackState.STOPPED)


def test_a_tap_on_a_lost_mirror_rebuilds_and_plays_the_queues_track(fake, output, monkeypatch):
    """The mirror lost while HQPlayer plays, a slot removed from the
    queue alone: a tap on the queue's slot 2 rebuilds HQPlayer's playlist and
    plays that slot's track — never the one the person removed."""
    mgr, b = output
    player_router = _routes_on(monkeypatch, mgr)
    _picker(monkeypatch, fake)
    b.lose_mirror("HQPlayer took 0 of 1 appended tracks")
    assert mgr.remove(2)                             # the queue's alone: [1, 3]
    player_router.jump(player_router.JumpRequest(index=2))
    assert _wait(lambda: fake.playlist == ["file:///E:/Music/A/01.flac",
                                           "file:///E:/Music/A/03.flac"]
                 and fake.state == int(PlaybackState.PLAYING) and fake.track == 2, timeout=10)
    assert _wait(lambda: mgr._latest_status.get("track_id") == "track-3", timeout=5)


def test_the_switch_away_stop_judges_what_hqplayer_was_given(fake, output, monkeypatch):
    """HQPlayer builds when the output is switched away, and another
    album plays elsewhere before the build ends: the Stop still goes — the
    entry it plays was ours when it was switched away from."""
    mgr, b = output
    _away(monkeypatch, fake)
    fake.build("SetFilter", 1.5, answer="last")
    threading.Thread(target=lambda: hb.change_dsp({"filter": 57, "filter1x": 57}),
                     daemon=True).start()
    assert _wait(lambda: _link().build.state is not None, timeout=3)
    n = len(fake.arrivals)
    mgr.activate(None)
    mgr.activate("browser")
    mgr.replace_queue([_item(f"E:/Music/Y/0{i}.flac", 50 + i) for i in (1, 2)], play=False)
    assert _wait(lambda: any(c == "Stop" for _, c in fake.arrivals[n:]), timeout=8)


@pytest.mark.parametrize("case", ["ours", "theirs", "back", "limit"])
def test_a_switch_away_from_a_silent_hqplayer_owes_its_stop(fake, output, monkeypatch, case):
    """HQPlayer silent with no build of ours when the output
    is switched away. The switch returns within a moment, saying so; once
    HQPlayer answers it stops if it plays ours — exactly once — and is left
    be when it plays another's, when the output came back to it first, or
    when it stayed silent past the limit."""
    from playback.base import PlayerBackend
    mgr, b = output
    _away(monkeypatch, fake)
    _limit(monkeypatch, 3.0)
    if case == "theirs":
        with fake._lock:
            fake.playlist.append("file:///Elsewhere/theirs.flac")
            fake.track = len(fake.playlist)
    fake.silence(6.0 if case == "limit" else 1.6)
    n = len(fake.arrivals)
    t0 = time.monotonic()
    told = mgr.activate(None)
    took = time.monotonic() - t0
    mgr.activate("browser")
    assert took < hl.BUILDING_AFTER_S + 0.4, took    # at once: the Stop stays owed
    assert told == getattr(hl, "STOP_OWED_SILENT", None)
    if case == "back":
        mgr.activate(None)
        mgr.activate("hqplayer")
    time.sleep(7.5 if case == "limit" else 3.0)
    stops = [c for _, c in fake.arrivals[n:] if c == "Stop"]
    assert len(stops) == (1 if case == "ours" else 0), stops
    assert isinstance(mgr.active, HqpBackend if case == "back" else _Away)
    if case == "back":
        mgr.active.shutdown()


def test_a_switch_away_from_an_answering_hqplayer_stops_it_before_the_new_output(fake, output,
                                                                               monkeypatch):
    """HQPlayer answering — the switch says nothing more, and
    its Stop went before the new output started."""
    mgr, b = output
    _away(monkeypatch, fake)
    order = []
    send = HQPlayerClient._send_command

    def sending(client, xml):
        order.append(xml.split()[0].strip("</>"))
        return send(client, xml)
    monkeypatch.setattr(HQPlayerClient, "_send_command", sending)
    monkeypatch.setattr(_Away, "start", lambda self: order.append("the new output"))
    n = len(fake.arrivals)
    t0 = time.monotonic()
    assert mgr.activate(None) is None
    assert time.monotonic() - t0 < 0.5
    mgr.activate("browser")
    assert "Stop" in order and order.index("Stop") < order.index("the new output"), order
    assert _wait(lambda: [c for _, c in fake.arrivals[n:]].count("Stop") == 1, timeout=3)


def test_a_reorder_on_a_lost_mirror_is_the_queues_alone(fake, output, monkeypatch):
    """A playlist that is no mirror is not edited by the queue's
    indices: the new order is the queue's alone, and the entry HQPlayer
    plays is never removed from under it."""
    mgr, b = output
    player_router = _routes_on(monkeypatch, mgr)
    b.lose_mirror("HQPlayer took 0 of 1 appended tracks")
    n = len(fake.commands)
    player_router.reorder(player_router.ReorderRequest(order=["track-1", "track-3", "track-2"]))
    assert [it.track_id for it in mgr.queue.snapshot()] == ["track-1", "track-3", "track-2"]
    assert [c for c, _ in fake.commands[n:] if c in ("PlaylistRemove", "PlaylistAdd")] == []
    assert (fake.state, fake.track) == (int(PlaybackState.PLAYING), 1)


def test_an_attach_inside_a_replace_loses_no_mirror(fake, output, monkeypatch):
    """The play gate re-attaching a stopped HQPlayer inside a replace
    takes the queue lock first: the attach mirrors the queue with no
    mutation in flight — no race to guard, no mirror lost for nothing."""
    mgr, b = output
    _picker(monkeypatch, fake)
    _stopped(mgr, fake)                              # the attach mirrors the queue
    b._gone = True                                   # the play gate re-attaches it
    added, _ = mgr.replace_queue([_item("E:/Music/B/01.flac", 11),
                                  _item("E:/Music/B/02.flac", 12)], play=True)
    try:
        assert added == 2 and mgr.active is not b
        assert mgr.active.mirror_losses == 0
        assert _wait(lambda: fake.playlist == ["file:///E:/Music/B/01.flac",
                                               "file:///E:/Music/B/02.flac"])
    finally:
        mgr.active.shutdown()


def test_re_picking_the_hqplayer_driven_lets_its_dsp_change_finish(fake, output, monkeypatch):
    """The HQPlayer already driven picked again stays the output as it
    is — the knob its build held back still goes."""
    mgr, b = output
    settings_router = _picker(monkeypatch, fake)
    fake.build("SetFilter", 1.5)                     # answered, then silent
    hb.change_dsp({"filter": 57, "filter1x": 57, "shaper": 2})
    _pick(settings_router, type="hqplayer", hqplayer={"host": "127.0.0.1", "port": fake.port})
    assert _wait(lambda: any(c == "SetShaping" for c, _ in fake.commands), timeout=8)
    assert mgr.active is b


def test_all_files_refused_at_an_attach_keep_the_queue_and_the_next_play_hands_it_over(
        fake, monkeypatch):
    """HQPlayer refusing every file it is handed at an attach —
    the disk unreachable — is no reason to empty the queue: it stays whole,
    no slot of it a gap, the mirror lost through the canary's first verdict
    over the empty playlist, and the next Play hands it over again."""
    from playback import manager as manager_mod
    mgr = PlaybackManager()
    monkeypatch.setattr(manager_mod, "manager", mgr)
    mgr.queue.replace([_item(f"E:/Music/R/{i:03d}.flac", 100 + i) for i in range(80)])
    fake.refuse.add("E:/Music/R/")
    judged = _canary_judged(monkeypatch)
    b = _attach(mgr)
    try:
        assert len(mgr.queue) == 80 and fake.playlist == []
        assert _wait(lambda: judged)                 # the poller's first answer: the canary
        assert b._mirror == "lost"
        assert not any(t.get("gap") for t in mgr.playlist()["tracks"])
        fake.refuse.clear()                          # the disk is back
        assert b.play()
        assert _wait(lambda: len(fake.playlist) == 80
                     and fake.state == int(PlaybackState.PLAYING), timeout=10)
    finally:
        b.shutdown()


def test_two_files_refused_of_eighty_at_a_replace_are_gaps_and_hqplayer_holds_78(fake, output):
    """What HQPlayer refuses of a batch it took the rest of stays in the
    queue, a gap of this HQPlayer named in the playlist: the queue all 80,
    HQPlayer the other 78 in order, one notice for the batch."""
    mgr, b = output
    items = [_item(f"E:/Music/R/{i:03d}.flac", 100 + i) for i in range(80)]
    fake.refuse.update({"R/017.flac", "R/049.flac"})
    added, _ = mgr.replace_queue(items, play=False)
    assert added == 80 and mgr.queue.snapshot() == items
    assert fake.playlist == [f"file:///E:/Music/R/{i:03d}.flac" for i in range(80)
                             if i not in (17, 49)]
    assert [i for i, t in enumerate(mgr.playlist()["tracks"])
            if t.get("gap") == hb.REFUSED_HERE] == [17, 49]
    assert b._mirror == "step"
    notice = diag.refused()
    assert notice["count"] == 2 and notice["total"] == 80


def test_a_replace_refused_whole_changes_nothing_and_play_gives_the_queue_back(fake, output):
    """A replace HQPlayer refused whole commits nothing — the
    queue, the resume spot and history as they were — and is answered in
    HQPlayer's words; since its Stop went (a file HQPlayer refuses clears
    nothing) the mirror is lost: the next Play gives HQPlayer the queue as
    it was, starting at the spot."""
    from playback.manager import QueueRefused
    mgr, b = output
    b.stop()
    _stopped(mgr, fake)
    before = mgr.queue.snapshot()
    b._set_spot(before[1], 30)
    rotated = []
    fake.refuse.add("E:/Music/Z/")
    with pytest.raises(QueueRefused, match="HQPlayer refused all 3 tracks of a new queue"):
        mgr.replace_queue([_item(f"E:/Music/Z/0{i}.flac", 70 + i) for i in (1, 2, 3)],
                          play=True, archive=lambda: rotated.append(True))
    assert rotated == [] and mgr.queue.snapshot() == before
    assert b._spot_at() == (2, 30)
    fake.commands.clear()
    assert b.play()
    assert _wait(lambda: fake.playlist == [f"file:///E:/Music/A/0{i}.flac" for i in (1, 2, 3)]
                 and fake.state == int(PlaybackState.PLAYING), timeout=10)
    assert [(c, a) for c, a in fake.commands if c in ("SelectTrack", "Seek")][:2] == [
        ("SelectTrack", {"index": "2"}), ("Seek", {"position": "30"})]


def test_an_append_of_two_refused_whole_leaves_the_queue_and_the_mirror(fake, output):
    """A person's append HQPlayer refused whole — it took
    nothing — leaves the queue as it was, and the mirror whole."""
    mgr, b = output
    fake.refuse.add("E:/Music/B/")
    assert mgr.append([_item("E:/Music/B/01.flac", 11), _item("E:/Music/B/02.flac", 12)]) == 0
    assert [it.track_id for it in mgr.queue.snapshot()] == ["track-1", "track-2", "track-3"]
    assert b._mirror == "step" and len(fake.playlist) == 3


def test_a_loss_while_the_canary_reads_is_not_undone_by_its_stale_verdict(fake, output,
                                                                          monkeypatch):
    """The canary judges HQPlayer's playlist with the mirror's state it read
    it with: a loss landing while it reads — a replace HQPlayer refused
    whole, its Stop gone — is not undone by a stale "in step"."""
    mgr, b = output
    armed = threading.Event()
    get_playlist = HQPlayerClient.get_playlist

    def reading(client, *a, **kw):
        out = get_playlist(client, *a, **kw)
        if armed.is_set() and threading.current_thread().name == "hqp-status-poller":
            armed.clear()
            with fake._lock:
                fake.playlist = []
            b.lose_mirror("HQPlayer refused all 3 tracks of a new queue", by="refusal")
        return out
    monkeypatch.setattr(HQPlayerClient, "get_playlist", reading)
    monkeypatch.setattr(hb, "DRIFT_CHECK_EVERY", 1)              # the canary on every tick
    armed.set()
    assert _wait(lambda: (b.poke(), not armed.is_set())[1], timeout=5)
    time.sleep(1.5)                                               # its next verdicts
    assert b._mirror == "lost"


def _lost_and_stopped(mgr, b, fake):
    _stopped(mgr, fake)
    b.lose_mirror("HQPlayer came back with a different playlist (restarted?)", by="refusal")
    fake.service["PlaylistAdd"] = 0.3                # a rebuild that takes its time


def test_a_double_play_during_a_rebuild_is_one_rebuild_and_one_start(fake, output):
    """A play press on a playlist that is no mirror rebuilds it
    in the background, answered at once; a second press while it does joins
    it — one rebuild, one start."""
    mgr, b = output
    _lost_and_stopped(mgr, b, fake)
    fake.commands.clear()
    t0 = time.monotonic()
    assert b.play()
    assert time.monotonic() - t0 < 0.5               # answered at once, "rebuilding"
    assert b.play()
    assert _wait(lambda: fake.state == int(PlaybackState.PLAYING), timeout=10)
    time.sleep(0.5)
    clears = [a for c, a in fake.commands if c == "PlaylistAdd" and a.get("clear") == "1"]
    starts = [c for c, _ in fake.commands if c in ("SelectTrack", "Play")]
    assert len(clears) == 1 and starts.count("SelectTrack") == 1, (clears, starts)


@pytest.mark.parametrize("when", ["before it begins", "during its adds"])
def test_a_pause_during_a_rebuild_drops_its_start(fake, output, when):
    """A Pause while the play press rebuilds — before the
    rebuild begins (it waits for a queue edit to commit), or while HQPlayer
    takes the queue — supersedes its start: HQPlayer takes the queue, and
    stays stopped."""
    mgr, b = output
    _lost_and_stopped(mgr, b, fake)
    fake.commands.clear()
    if when == "before it begins":
        with mgr._mutate_lock:                       # a queue edit still committing
            assert b.play()
            b.pause()
    else:
        assert b.play()
        assert _wait(lambda: any(c == "PlaylistAdd" for c, _ in fake.commands), timeout=5)
        b.pause()
    assert _wait(lambda: b._pressed is None, timeout=5)
    time.sleep(0.5)                                  # what a start not dropped would send
    assert [a.get("clear") for c, a in fake.commands if c == "PlaylistAdd"] == ["1", "0", "0"]
    assert fake.state != int(PlaybackState.PLAYING)
    assert "SelectTrack" not in [c for c, _ in fake.commands]


def test_play_on_a_pause_with_the_mirror_lost_only_resumes(fake, output):
    """Play on a pause of our track with the mirror lost
    resumes it — the sound never stops for a rebuild."""
    mgr, b = output
    with fake._lock:
        fake.state = int(PlaybackState.PAUSED)
    assert _wait(lambda: mgr._latest_status.get("state") == "paused")
    b.lose_mirror("HQPlayer took 0 of 1 appended tracks")
    fake.commands.clear()
    assert b.play()
    time.sleep(1.0)
    assert "Stop" not in [c for c, _ in fake.commands]
    assert fake.state == int(PlaybackState.PLAYING) and fake.track == 1


def _tail(mgr, b, fake):
    """A tail mirror: HQPlayer holds entries of its own before and behind
    the one it reads — [1, x, 2, y, 3] while the queue is [1, 2, 3, 4] — and
    plays 2: what lies behind it is replaced, what lies before it stays."""
    mgr.queue.append([_item("E:/Music/A/04.flac", 4)])
    with fake._lock:
        fake.playlist = ["file:///E:/Music/A/01.flac", "file:///E:/Music/X/09.flac",
                         "file:///E:/Music/A/02.flac", "file:///E:/Music/X/10.flac",
                         "file:///E:/Music/A/03.flac"]
        fake.track, fake.state, fake.position = 3, int(PlaybackState.PLAYING), 10.0
    b.lose_mirror("HQPlayer took 0 of 1 appended tracks")
    assert _wait(lambda: fake.playlist == [
        "file:///E:/Music/A/01.flac", "file:///E:/Music/X/09.flac", "file:///E:/Music/A/02.flac",
        "file:///E:/Music/A/03.flac", "file:///E:/Music/A/04.flac"]
        and b._mirror == "tail", timeout=5), (fake.playlist, b._mirror)
    assert fake.track == 3
    fake.commands.clear()


def test_a_tail_mirror_follows_two_natural_advances(fake, output, listens):
    """I14: in a tail mirror HQPlayer's own advances play the queue's next
    tracks — the slot is read off the entry, never HQPlayer's index — and
    nothing stops or clears it."""
    mgr, b = output
    _tail(mgr, b, fake)
    for track, want in ((4, "track-3"), (5, "track-4")):
        with fake._lock:
            fake.track, fake.position = track, 0.0
        assert _wait(lambda: mgr._latest_status.get("track_id") == want, timeout=5), want
    assert not [c for c, a in fake.commands
                if c == "Stop" or (c == "PlaylistAdd" and a.get("clear") == "1")]


def test_an_append_on_a_tail_mirror_reaches_hqplayer_behind_the_tail(fake, output):
    """I14: what touches only the tail goes to HQPlayer as on a mirror."""
    mgr, b = output
    _tail(mgr, b, fake)
    assert mgr.append([_item("E:/Music/A/05.flac", 5)]) == 1
    assert fake.playlist[-2:] == ["file:///E:/Music/A/04.flac", "file:///E:/Music/A/05.flac"]


def test_previous_on_a_tail_mirror_rebuilds_and_starts_the_right_slot(fake, output):
    """I14: a person's action that picks a slot ends the tail mirror — a
    full rebuild, the start at the queue's slot before."""
    mgr, b = output
    _tail(mgr, b, fake)
    assert b.previous()
    assert _wait(lambda: fake.playlist == [f"file:///E:/Music/A/0{i}.flac" for i in (1, 2, 3, 4)]
                 and fake.state == int(PlaybackState.PLAYING) and fake.track == 1, timeout=10)


def test_a_switch_away_cancels_the_knobs_a_build_held_back(fake, output, monkeypatch):
    """I3: the knobs of a DSP change still to go after its build are
    superseded by a switch away — never sent to the HQPlayer left."""
    mgr, b = output
    _away(monkeypatch, fake)
    fake.build("SetFilter", 1.5)
    hb.change_dsp({"filter": 57, "filter1x": 57, "shaper": 2})
    mgr.activate(None)
    mgr.activate("browser")
    time.sleep(2.5)
    assert "SetShaping" not in [c for c, _ in fake.commands]


@pytest.mark.parametrize("press", ["pause", "stop"])
def test_a_pause_or_a_stop_supersedes_a_start_left_for_later(fake, output, press):
    """I3: a person's Pause or Stop is a transport intent — a start made
    before it, left for later, is never sent after it."""
    mgr, b = output
    link = _link()
    start = link.item("start", link.accept("queue"))
    assert getattr(b, press)()
    with pytest.raises(hl.Dropped, match="a newer request came first"):
        with link.command(item=start):
            link.client.play()


# -- Gaps (2026-10-13) ----------------------------------------------------------------
# A slot this HQPlayer does not hold — nothing here it can open, or a file it
# refused while it took the rest of the batch — stays in the queue, named,
# and is never handed to it: HQPlayer's playlist is the queue without its
# gaps, in the same order.

def _ways_in_by_library(items, output_id, endpoint_id):
    """playback.substitute.native_plays as the database answers it here: a
    file held in an HQPlayer's own library opens on that HQPlayer alone, and
    no other copy of it is anywhere."""
    return [None if it.source["kind"] != "hqp" or it.source["endpoint"] == endpoint_id
            else {"kind": "unplayable", "reason": "held in another HQPlayer's library"}
            for it in items]


def _held_by(path, n, endpoint_id):
    return QueueItem(track_id=f"track-{n}", media_file_id=None,
                     source={"kind": "hqp", "path": path, "format": "FLAC",
                             "endpoint": endpoint_id},
                     title=f"Song {n}", artist="Artist", album="Album")


@pytest.fixture
def two_libraries(fake, fake_b, monkeypatch):
    """Two HQPlayers, each with a library of its own: `fake` is endpoint 5
    (it holds the L/ files), `fake_b` endpoint 7, which refuses a path it
    does not hold. A manager drives `fake`, stopped, its queue L1, a, L2, b."""
    import hqp_library
    from playback import manager as manager_mod
    from playback import substitute
    monkeypatch.setattr(hqp_library, "endpoint_by_address",
                        lambda host, port: {"id": 5 if port == fake.port else 7, "name": "fake"})
    monkeypatch.setattr(substitute, "native_plays", _ways_in_by_library)
    fake_b.refuse.add("Library/L/")
    mgr = PlaybackManager()
    monkeypatch.setattr(manager_mod, "manager", mgr)
    queue = [_held_by("D:/Library/L/01.flac", 1, 5), _item("E:/Music/A/02.flac", 2),
             _held_by("D:/Library/L/03.flac", 3, 5), _item("E:/Music/A/04.flac", 4)]
    mgr.queue.replace(queue)
    settings_router = _picker(monkeypatch, fake)
    mgr.activate("hqplayer")
    yield mgr, settings_router, queue
    if mgr.active is not None:
        mgr.active.shutdown()


def test_the_slots_of_another_hqplayers_library_are_gaps_and_the_queue_survives_the_round_trip(
        fake, fake_b, two_libraries):
    """From one HQPlayer to another and back: on the one that does not hold
    the files of the other's library the queue is the same, those slots left
    out of its playlist and named; back on the first, it holds them all
    again."""
    mgr, settings_router, queue = two_libraries
    assert fake.playlist == ["file:///D:/Library/L/01.flac", "file:///E:/Music/A/02.flac",
                             "file:///D:/Library/L/03.flac", "file:///E:/Music/A/04.flac"]
    _pick(settings_router, type="hqplayer", hqplayer={"host": "127.0.0.1", "port": fake_b.port})
    assert mgr.queue.snapshot() == queue
    assert fake_b.playlist == ["file:///E:/Music/A/02.flac", "file:///E:/Music/A/04.flac"]
    assert not any(c == "PlaylistAdd" and "Library/L/" in a["uri"] for c, a in fake_b.commands)
    assert [t.get("gap") for t in mgr.playlist()["tracks"]] == [
        "held in another HQPlayer's library", None, "held in another HQPlayer's library", None]
    _pick(settings_router, type="hqplayer", hqplayer={"host": "127.0.0.1", "port": fake.port})
    assert mgr.queue.snapshot() == queue
    assert fake.playlist == ["file:///D:/Library/L/01.flac", "file:///E:/Music/A/02.flac",
                             "file:///D:/Library/L/03.flac", "file:///E:/Music/A/04.flac"]
    assert not any(t.get("gap") for t in mgr.playlist()["tracks"])


def test_a_tap_on_a_slot_after_two_gaps_plays_that_track(fake, fake_b, two_libraries):
    """The queue's slot is translated to HQPlayer's entry: a tap on the slot
    after two gaps starts that very track, and a tap on a gap is refused in
    its words, nothing sent."""
    mgr, settings_router, queue = two_libraries
    mgr.queue.replace([_held_by("D:/Library/L/05.flac", 5, 5), _item("E:/Music/A/06.flac", 6),
                       _held_by("D:/Library/L/07.flac", 7, 5),
                       _held_by("D:/Library/L/08.flac", 8, 5), _item("E:/Music/A/09.flac", 9)])
    _pick(settings_router, type="hqplayer", hqplayer={"host": "127.0.0.1", "port": fake_b.port})
    assert fake_b.playlist == ["file:///E:/Music/A/06.flac", "file:///E:/Music/A/09.flac"]
    b = mgr.active
    fake_b.commands.clear()
    assert not b.select(3)
    assert "held in another HQPlayer's library" in b.refusal()
    assert not [c for c, _ in fake_b.commands if c in ("SelectTrack", "Play")]
    assert b.select(5)
    assert ("SelectTrack", {"index": "2"}) in fake_b.commands
    assert fake_b.playlist[fake_b.track - 1] == "file:///E:/Music/A/09.flac"
    assert _wait(lambda: mgr._latest_status.get("track_index") == 5)


def test_a_remove_and_a_reorder_around_a_gap_keep_hqplayer_on_its_entries(
        fake, fake_b, two_libraries, monkeypatch):
    """A slot HQPlayer holds is removed at its own entry, a gap from the
    queue alone; a new order behind the playing slot moves HQPlayer's
    entries, the gap left out — the playing entry never touched."""
    mgr, settings_router, queue = two_libraries
    from routers import player as player_router
    monkeypatch.setattr(player_router, "manager", mgr)
    mgr.queue.replace([_item("E:/Music/A/02.flac", 2), _held_by("D:/Library/L/01.flac", 1, 5),
                       _item("E:/Music/A/04.flac", 4), _item("E:/Music/A/05.flac", 5),
                       _item("E:/Music/A/06.flac", 6)])
    _pick(settings_router, type="hqplayer", hqplayer={"host": "127.0.0.1", "port": fake_b.port})
    assert fake_b.playlist == [f"file:///E:/Music/A/0{i}.flac" for i in (2, 4, 5, 6)]
    with fake_b._lock:
        fake_b.state, fake_b.track = int(PlaybackState.PLAYING), 1
    assert _wait(lambda: mgr._latest_status.get("track_index") == 1)
    fake_b.commands.clear()
    assert mgr.remove(4, "track-5")                     # behind the gap
    assert [a["index"] for c, a in fake_b.commands if c == "PlaylistRemove"] == ["3"]
    assert mgr.remove(2, "track-1")                     # the gap itself
    assert [a["index"] for c, a in fake_b.commands if c == "PlaylistRemove"] == ["3"]
    assert fake_b.playlist == [f"file:///E:/Music/A/0{i}.flac" for i in (2, 4, 6)]
    assert mgr.append([_held_by("D:/Library/L/03.flac", 3, 5)], "next") == 1   # a gap again
    assert fake_b.playlist == [f"file:///E:/Music/A/0{i}.flac" for i in (2, 4, 6)]
    fake_b.commands.clear()
    player_router.reorder(player_router.ReorderRequest(
        order=["track-2", "track-6", "track-3", "track-4"]))
    assert fake_b.playlist == [f"file:///E:/Music/A/0{i}.flac" for i in (2, 6, 4)]
    assert "Stop" not in [c for c, _ in fake_b.commands]
    assert (fake_b.state, fake_b.track) == (int(PlaybackState.PLAYING), 1)
    assert [it.track_id for it in mgr.queue.snapshot()] == ["track-2", "track-6", "track-3",
                                                            "track-4"]


def test_a_file_gone_is_a_gap_and_the_next_mirror_takes_it_back(fake, output, monkeypatch):
    """A slot whose file left the library with no copy HQPlayer opens is a
    gap: its entry leaves HQPlayer's playlist, the slot stays in the queue,
    named. Back in the library, the next full mirror hands it over again."""
    from playback import substitute
    mgr, b = output
    back = {}

    def ways_in(items, output_id, endpoint_id):
        return [None if it.source["kind"] != "track"
                else {**back[it.track_id], "kind": "file"} if it.track_id in back
                else {"kind": "unplayable", "reason": "its file left the library"}
                for it in items]
    monkeypatch.setattr(substitute, "native_plays", ways_in)
    b.stop()
    _stopped(mgr, fake)
    with mgr._mutation():
        moved = mgr.queue.rebind(
            lambda items: [{"media_file_id": None, "source": {"kind": "track"}}
                           if i == 2 else None for i, _ in enumerate(items, start=1)],
            lambda items: ways_in(items, "hqplayer", 7))
        assert len(moved) == 1
        b.queue_changed("rebind")
    assert fake.playlist == ["file:///E:/Music/A/01.flac", "file:///E:/Music/A/03.flac"]
    assert [it.track_id for it in mgr.queue.snapshot()] == ["track-1", "track-2", "track-3"]
    assert [t.get("gap") for t in mgr.playlist()["tracks"]] == [
        None, "its file left the library", None]
    back["track-2"] = {"path": "E:/Music/B/02.flac", "format": "FLAC", "media_file_id": 22}
    fake.restart()                                   # the next mirror: HQPlayer back, stopped
    assert _wait(lambda: fake.playlist == ["file:///E:/Music/A/01.flac",
                                           "file:///E:/Music/B/02.flac",
                                           "file:///E:/Music/A/03.flac"], timeout=10)
    assert not any(t.get("gap") for t in mgr.playlist()["tracks"])


def test_only_hqplayers_own_entries_reach_its_index_commands():
    """HQPlayer's playlist index is a type of its own (Entry): the client's
    SelectTrack and PlaylistRemove are reached through _select and _remove
    alone, and a queue slot handed to either is refused."""
    import ast
    tree = ast.parse(Path(hb.__file__).read_text())
    callers = {}
    for fn in ast.walk(tree):
        if isinstance(fn, (ast.FunctionDef, ast.Lambda)):
            for node in ast.walk(fn):
                if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                        and node.func.attr in ("select_track", "playlist_remove")):
                    callers.setdefault(node.func.attr, set()).add(
                        getattr(fn, "name", "<lambda>"))
    assert callers == {"select_track": {"_select"}, "playlist_remove": {"_remove"}}
    with pytest.raises(TypeError):
        hb._select(None, 2)
    with pytest.raises(TypeError):
        hb._remove(None, 2)


def test_a_file_refused_before_the_kept_slot_leaves_the_spot_on_its_track(fake, monkeypatch):
    """Switched to HQPlayer stopped, the slot the old output was on is the
    spot. A file before it HQPlayer refuses is a gap — the queue whole — and
    Play starts the very track the person was on, at HQPlayer's own entry."""
    from playback import manager as manager_mod
    mgr = PlaybackManager()
    monkeypatch.setattr(manager_mod, "manager", mgr)
    _picker(monkeypatch, fake)
    mgr.queue.replace([_item(f"E:/Music/A/0{i}.flac", i) for i in range(1, 6)])
    mgr._last_slot = 4                                   # the old output was on track-4
    fake.refuse.add("A/02.flac")
    mgr.activate("hqplayer")
    b = mgr.active
    try:
        assert [it.track_id for it in mgr.queue.snapshot()] == [f"track-{i}" for i in range(1, 6)]
        assert b._spot_at() == (4, 0)
        assert b.play()
        assert ("SelectTrack", {"index": "3"}) in fake.commands
        assert fake.playlist[fake.track - 1] == "file:///E:/Music/A/04.flac"
    finally:
        b.shutdown()


def test_a_file_refused_in_a_reorders_rebuild_is_a_gap(fake, output, monkeypatch):
    """A reorder's rebuild is a full mirror: what HQPlayer refuses of it is a
    gap — the queue in its new order, the refused track in it and named — and
    the playlist the rest, in step, one notice."""
    mgr, b = output
    player_router = _routes_on(monkeypatch, mgr)
    fake.refuse.add("A/03.flac")
    out = player_router.reorder(player_router.ReorderRequest(
        order=["track-2", "track-1", "track-3"]))
    assert out["interrupted"]                            # a rebuild: the current track moves right
    assert fake.playlist == ["file:///E:/Music/A/02.flac", "file:///E:/Music/A/01.flac"]
    assert [it.track_id for it in mgr.queue.snapshot()] == ["track-2", "track-1", "track-3"]
    assert b._mirror == "step" and diag.refused()["count"] == 1
    assert [t.get("gap") for t in mgr.playlist()["tracks"]] == [None, None, hb.REFUSED_HERE]


def test_a_replace_cut_short_by_a_silence_keeps_every_track_and_loses_the_mirror(fake, output):
    """An add HQPlayer leaves unanswered is carried out once it answers
    again: a replace cut short there is no mirror in step. The queue takes
    every track — the person asked for all of them — and HQPlayer gets the
    rest at its next answer, in step after it."""
    mgr, b = output
    _link().client.timeout = 0.5                       # an add's wait, shortened
    dispatch = fake._dispatch
    adds = itertools.count(1)

    def slow_second_add(cmd, attrs):
        if cmd == "PlaylistAdd" and next(adds) == 2:
            time.sleep(1.0)                            # busy: answered late, carried out all the same
        return dispatch(cmd, attrs)
    fake._dispatch = slow_second_add
    items = [_item(f"E:/Music/B/0{i}.flac", 10 + i) for i in (1, 2, 3)]
    added, _ = mgr.replace_queue(items, play=False)
    assert added == 3 and mgr.queue.snapshot() == items
    assert b._mirror == "lost"
    assert _wait(lambda: fake.playlist == [f"file:///E:/Music/B/0{i}.flac" for i in (1, 2, 3)]
                 and b._mirror == "step", timeout=10), (fake.playlist, b._mirror)


def test_stopped_in_a_tail_mirror_the_slot_and_play_stay_with_the_track(fake, output):
    """The slot a stopped HQPlayer stands on is named by what its entry is,
    never by its place: stopped in a tail mirror on the queue's slot 2
    (HQPlayer's entry 3), the status names slot 2's track, and Play starts
    there — as a Play after a Stop does on a mirror — not at slot 1."""
    mgr, b = output
    _tail(mgr, b, fake)          # HQPlayer [01, X09, 02, 03, 04] reads entry 3 = queue slot 2
    assert _wait(lambda: mgr._latest_status.get("track_id") == "track-2", timeout=5)
    assert b.stop()
    assert _wait(lambda: mgr._latest_status.get("state") == "stopped", timeout=5)
    named = mgr._latest_status.get("track_id")
    assert b.play()
    assert _wait(lambda: fake.state == int(PlaybackState.PLAYING) and b._pressed is None, timeout=10)
    assert _wait(lambda: mgr._latest_status.get("track_id") == "track-2", timeout=5)
    assert (named, fake.playlist[fake.track - 1]) == ("track-2", "file:///E:/Music/A/02.flac")


# The backend's own mirror steps judge the queue they act on: the guard that
# keeps the poller from naming an entry while a mutation is in flight is not
# theirs — in a tail mirror HQPlayer's entry and the queue's slot differ always.

def _named(mgr, track_id):
    """The poller has named the slot HQPlayer reads, for the queue as it stands."""
    return _wait(lambda: mgr._latest_status.get("track_id") == track_id
                 and mgr._latest_status.get("playlist_version") == mgr.queue.version, timeout=5)


def test_two_quick_removes_behind_the_entry_read_both_reach_a_tail_mirror(fake, output):
    """A remove behind the slot HQPlayer reads goes to it at the entry it
    holds there — two in a row, as two taps within a second."""
    mgr, b = output
    _tail(mgr, b, fake)          # HQPlayer [01, X09, 02, 03, 04] reads entry 3 = queue slot 2
    assert _named(mgr, "track-2")
    assert mgr.remove(4)         # track-4
    assert mgr.remove(3)         # track-3, right after
    assert [it.track_id for it in mgr.queue.snapshot()] == ["track-1", "track-2"]
    assert b._mirror == "tail"
    assert fake.playlist == ["file:///E:/Music/A/01.flac", "file:///E:/Music/X/09.flac",
                             "file:///E:/Music/A/02.flac"], fake.playlist


def test_a_remove_right_after_hqplayers_own_advance_reaches_a_tail_mirror(fake, output):
    """HQPlayer advanced on its own to entry 4 (the queue's slot 3), and the
    person removes slot 4 before the next status tick."""
    mgr, b = output
    _tail(mgr, b, fake)
    assert _named(mgr, "track-2")
    b._running = False                   # no tick between the advance and the remove
    b._wake.set()
    b._thread.join(timeout=3)
    with fake._lock:
        fake.track, fake.position = 4, 0.0
    assert mgr.remove(4)                 # track-4, behind the slot HQPlayer reads now
    assert [it.track_id for it in mgr.queue.snapshot()] == ["track-1", "track-2", "track-3"]
    assert fake.playlist == ["file:///E:/Music/A/01.flac", "file:///E:/Music/X/09.flac",
                             "file:///E:/Music/A/02.flac", "file:///E:/Music/A/03.flac"], fake.playlist


def test_a_play_next_after_a_remove_lands_behind_the_entry_a_tail_mirror_reads(fake, output):
    """A play-next on a tail mirror goes to HQPlayer right behind the entry
    it reads, where the queue takes it."""
    mgr, b = output
    _tail(mgr, b, fake)
    assert _named(mgr, "track-2")
    assert mgr.remove(4)                                      # track-4
    assert mgr.append([_item("E:/Music/A/07.flac", 7)], position="next") == 1
    assert [it.track_id for it in mgr.queue.snapshot()] == ["track-1", "track-2", "track-7",
                                                            "track-3"]
    assert fake.playlist[2:] == ["file:///E:/Music/A/02.flac", "file:///E:/Music/A/07.flac",
                                 "file:///E:/Music/A/03.flac"], fake.playlist


def test_a_reorder_behind_the_entry_read_after_a_remove_reaches_a_tail_mirror(fake, output,
                                                                               monkeypatch):
    """A new order behind the slot HQPlayer reads goes to it behind the
    entry it reads — also right after another edit moved the queue on."""
    mgr, b = output
    player_router = _routes_on(monkeypatch, mgr)
    _tail(mgr, b, fake)          # HQPlayer [01, X09, 02, 03, 04] reads entry 3 = queue slot 2
    assert _named(mgr, "track-2")
    assert mgr.append([_item("E:/Music/A/05.flac", 5)]) == 1     # reaches HQPlayer behind the tail
    assert fake.playlist[-1] == "file:///E:/Music/A/05.flac"
    assert mgr.remove(5)         # track-5, right after
    player_router.reorder(player_router.ReorderRequest(
        order=["track-1", "track-2", "track-4", "track-3"]))
    assert (b._mirror, fake.playlist[2:]) == ("tail", [
        "file:///E:/Music/A/02.flac", "file:///E:/Music/A/04.flac", "file:///E:/Music/A/03.flac"])


def test_a_commit_between_hqplayers_answer_and_its_reconverge_leaves_no_mirror_lost(fake, output):
    """Lost through a silence, HQPlayer playing an entry of ours gets what
    lies behind it at its next answer, once per loss: a queue edit that
    commits between that answer and the reconverge (a filler's append) does
    not spend the once for nothing."""
    mgr, b = output
    with fake._lock:
        fake.track, fake.position = 3, 0.0                       # HQPlayer reads entry 3 = track-3
    assert _wait(lambda: mgr._latest_status.get("track_id") == "track-3")
    with mgr._mutate_lock:
        b.lose_mirror("HQPlayer took 0 of 1 appended tracks")   # through a silence
        assert mgr.remove(1)                                     # the queue's alone: [2, 3]
        # the poller answers and starts the reconverge, which waits for the queue lock
        assert _wait(lambda: any(t.name == "hqp-reconverge" for t in threading.enumerate()),
                     timeout=5)
        assert mgr.append([_item("E:/Music/A/04.flac", 4)]) == 1  # the queue's alone
    assert _wait(lambda: fake.playlist == [f"file:///E:/Music/A/0{i}.flac" for i in (1, 2, 3, 4)]
                 and b._mirror == "tail", timeout=5), (fake.playlist, b._mirror)


def test_the_refusal_notice_ends_once_a_play_press_hands_the_whole_queue_over(fake, monkeypatch):
    """A batch HQPlayer takes whole ends the refusal notice of the last one:
    all refused at an attach (the disk away) raises it; the disk back, the
    play press hands HQPlayer the whole queue and the notice goes."""
    from playback import manager as manager_mod
    mgr = PlaybackManager()
    monkeypatch.setattr(manager_mod, "manager", mgr)
    mgr.queue.replace([_item(f"E:/Music/R/{i:03d}.flac", 100 + i) for i in range(5)])
    fake.refuse.add("E:/Music/R/")
    b = _attach(mgr)
    try:
        assert diag.refused() is not None and diag.refused()["count"] == 5
        fake.refuse.clear()                          # the disk is back
        assert b.play()
        assert _wait(lambda: len(fake.playlist) == 5
                     and fake.state == int(PlaybackState.PLAYING), timeout=10)
        assert _wait(lambda: b._pressed is None, timeout=5)
        assert b._mirror == "step"
        assert diag.refused() is None, diag.refused()
    finally:
        b.shutdown()


def test_a_stopped_hqplayer_given_the_queue_back_keeps_its_track_as_the_spot(fake, output):
    """Lost through a silence, a stopped HQPlayer is given the queue afresh
    and the play press resumes the slot of the track it stood on — named by
    what its entry is, not by its place in a playlist that was no mirror."""
    mgr, b = output
    mgr.append([_item("E:/Music/A/04.flac", 4)])
    assert fake.playlist[-1] == "file:///E:/Music/A/04.flac"
    with fake._lock:
        fake.track, fake.position = 3, 0.0
    assert _wait(lambda: mgr._latest_status.get("track_id") == "track-3")
    b.stop()
    _stopped(mgr, fake)                                   # stopped on track-3 (entry 3)
    with mgr._mutate_lock:
        b.lose_mirror("HQPlayer took 0 of 1 appended tracks")
        assert mgr.remove(1)                               # the queue's alone: [2, 3, 4]
    assert _wait(lambda: b._mirror == "step", timeout=5), b._mirror
    spot = b._spot_at()
    assert spot is not None and mgr.queue.item_at(spot[0]).track_id == "track-3", (
        spot, spot and mgr.queue.item_at(spot[0]).track_id)


def test_a_full_mirror_whose_first_file_is_refused_still_clears_the_playlist(fake, output):
    """The clear of a full mirror rides on the first add HQPlayer takes: a
    first file it refuses clears nothing, and what follows must not land
    behind the playlist it held."""
    mgr, b = output
    fake.refuse.add("B/01.flac")
    items = [_item(f"E:/Music/B/0{i}.flac", 20 + i) for i in (1, 2, 3)]
    added, _ = mgr.replace_queue(items, play=False)
    assert fake.playlist == ["file:///E:/Music/B/02.flac", "file:///E:/Music/B/03.flac"]
    assert added == 3 and b._mirror == "step"


def test_a_refused_file_re_bound_to_another_copy_is_handed_over(fake, output):
    """A refusal is of a file: a slot whose refused file is re-bound to
    another copy of its track is no gap any more, and HQPlayer's playlist
    follows the re-bind with the new copy in it."""
    mgr, b = output
    b.stop()
    _stopped(mgr, fake)
    fake.refuse.add("A/02.flac")
    fake.restart()                                   # back stopped: the queue afresh
    assert _wait(lambda: fake.playlist == ["file:///E:/Music/A/01.flac",
                                           "file:///E:/Music/A/03.flac"]
                 and b._mirror == "step", timeout=10)
    assert [it.track_id for it in mgr.queue.snapshot()] == ["track-1", "track-2", "track-3"]
    assert b.gap(mgr.queue.item_at(2)) == hb.REFUSED_HERE
    with mgr._mutation():
        _move(mgr, 2, "E:/Music/B/02.flac", 20)
        b.queue_changed("rebind")
    assert b.gap(mgr.queue.item_at(2)) is None
    assert fake.playlist == ["file:///E:/Music/A/01.flac", "file:///E:/Music/B/02.flac",
                             "file:///E:/Music/A/03.flac"]


def test_a_track_added_while_the_mirror_is_lost_is_read_for_this_hqplayer(fake, fake_b,
                                                                          two_libraries):
    """An append the queue takes alone, the mirror lost, reads its way in
    for the HQPlayer driven all the same: a file of the other's library is a
    gap named at once, and HQPlayer is never handed it when it gets the queue
    back."""
    mgr, settings_router, queue = two_libraries
    _pick(settings_router, type="hqplayer", hqplayer={"host": "127.0.0.1", "port": fake_b.port})
    assert mgr.queue.snapshot() == queue
    b = mgr.active
    with fake_b._lock:
        fake_b.state, fake_b.track = int(PlaybackState.PLAYING), 1
    with mgr._mutate_lock:
        b.lose_mirror("HQPlayer took 0 of 1 appended tracks")
        assert mgr.append([_held_by("D:/Library/L/09.flac", 9, 5)]) == 1
        assert mgr.playlist()["tracks"][-1].get("gap") == "held in another HQPlayer's library"
    assert _wait(lambda: b._mirror in ("step", "tail"), timeout=5), b._mirror
    assert not any(c == "PlaylistAdd" and "Library/L/" in a["uri"] for c, a in fake_b.commands)


def test_a_spot_on_a_gap_starts_the_next_slot_hqplayer_holds_from_its_start(fake, fake_b,
                                                                            two_libraries):
    """Play from a spot on a slot this HQPlayer does not hold starts the
    next one it holds, from 0 s: the second belonged to the track the spot
    was taken on."""
    mgr, settings_router, queue = two_libraries
    _pick(settings_router, type="hqplayer", hqplayer={"host": "127.0.0.1", "port": fake_b.port})
    b = mgr.active
    b._set_spot(queue[2], 90)                           # a file of the other library
    fake_b.commands.clear()
    assert b.play()
    assert [(c, a) for c, a in fake_b.commands if c in ("SelectTrack", "Seek")] == [
        ("SelectTrack", {"index": "2"})]
    assert fake_b.playlist[fake_b.track - 1] == "file:///E:/Music/A/04.flac"


# -- The connection's waits, ended from another thread (2026-10-13) ------------------
# A socket closed by a thread that does not read it races the one that does:
# every wait — a read, a connect — watches a wake channel beside its socket,
# and the thread that waits closes its own socket once woken.

def _closes(monkeypatch) -> list:
    """Every socket.close from here on: (the thread that made it, the socket)."""
    made = []
    close = socket.socket.close

    def recorded(sock):
        made.append((threading.current_thread().name, sock))
        close(sock)
    monkeypatch.setattr(socket.socket, "close", recorded)
    return made


def test_the_node_stop_closes_no_socket_a_request_reads(fake, monkeypatch):
    """The node stop wakes a request waiting on HQPlayer — a DSP change's
    answer held through a build — and the request's own thread closes the
    socket: the stopping thread closes none another thread reads."""
    _limit(monkeypatch, 30.0)
    link = _link()
    fake.build("SetFilter", 30.0, answer="last")
    assert hb.change_dsp({"filter": 57, "filter1x": 57})["building"] is not None
    change = next(t for t in threading.enumerate() if t.name == "hqp-dsp-change")
    held = link.client.socket
    made = _closes(monkeypatch)
    t0 = time.monotonic()
    hl.close_all("the node is stopping")
    change.join(2)
    assert not change.is_alive() and time.monotonic() - t0 < 2
    assert [who for who, sock in made if sock is held] == ["hqp-dsp-change"]


@pytest.mark.parametrize("run", range(int(os.environ.get("SAUTIUM_LOOP_RUNS", "3"))))
def test_the_node_stop_lets_go_of_everything_within_two_seconds_again_and_again(
        fake, output, monkeypatch, run):
    """The node stop of the test above, run again and again: a socket a
    close raced lost a read its whole wait now and then
    (SAUTIUM_LOOP_RUNS=40 for a long run)."""
    test_the_node_stop_lets_go_of_everything_within_two_seconds(fake, output, monkeypatch)


@pytest.mark.skipif(sys.platform == "win32",
                    reason="Windows' select() limits descriptors by count, not by number")
def test_a_request_and_its_wake_up_work_on_descriptors_past_1023(fake):
    """The waits go through the selectors module's default (epoll, kqueue),
    never select.select, which refuses a descriptor past 1023: a link made
    with a thousand files open answers, and a wake-up ends its wait."""
    import resource
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    if soft < 2048:
        if hard != resource.RLIM_INFINITY and hard < 2048:
            pytest.skip("this process may not open 2048 descriptors")
        resource.setrlimit(resource.RLIMIT_NOFILE, (2048, hard))
    held = []
    try:
        while not held or held[-1] < 1100:
            held.extend(os.pipe())
        link = _link()                                   # made now: its descriptors are high
        with link.asking() as c:
            assert c.get_status() is not None
        assert link.client.socket.fileno() > 1023 and link.client._waker.fileno() > 1023
        fake.silence(10)
        threading.Timer(0.3, link.client.interrupt).start()
        t0 = time.monotonic()
        with link.asking() as c:
            assert c.get_status(wait=10) is None
        assert time.monotonic() - t0 < 2
    finally:
        for fd in held:
            os.close(fd)
        resource.setrlimit(resource.RLIMIT_NOFILE, (soft, hard))


def test_an_interrupt_between_the_send_and_the_wait_ends_the_wait_at_once(fake, monkeypatch):
    """A wake-up given after the request went and before its wait began is
    not lost: the wait ends at once. Its flag and byte stay until the owner
    acts on them — the wait that met it — never drained at a wait's start."""
    link = _link()
    with link.asking() as c:
        assert c.get_status() is not None
    fake.silence(5)
    send = HQPlayerClient._send_command

    def sent_then_woken(client, xml):
        ok = send(client, xml)
        client.interrupt()
        return ok
    monkeypatch.setattr(HQPlayerClient, "_send_command", sent_then_woken)
    t0 = time.monotonic()
    with link.asking() as c:
        assert c.get_status(wait=5) is None
        assert c.last_error.result == "interrupted"
    assert time.monotonic() - t0 < 1
    assert not link.client.interrupted()             # spent by the wait that met it


def test_a_close_ends_a_connect_to_an_address_that_never_answers(monkeypatch):
    """A connect is a wait like a read's: a close ends one waiting on an
    address that drops every packet — never after the connect's own two
    seconds — and the request is let go, no failure of HQPlayer's."""
    import hqplayer_client
    monkeypatch.setattr(hqplayer_client, "connect_address", lambda host: "192.0.2.1")
    _fresh_links()
    link = hl.link_for("unreachable.example", 4321)
    connecting = threading.Event()
    connect_ex = socket.socket.connect_ex

    def watched(sock, address):
        err = connect_ex(sock, address)
        if err in hqplayer_client._CONNECTING:
            connecting.set()                         # under way: nothing answered yet
        return err
    monkeypatch.setattr(socket.socket, "connect_ex", watched)
    out = {}

    def ask():
        try:
            with link.asking() as c:
                c.get_status()
        except Exception as e:
            out["error"] = e
    t = threading.Thread(target=ask, daemon=True)
    t.start()
    try:
        if not connecting.wait(1.0):
            pytest.skip("this host does not route TEST-NET-1: the connect fails at once")
        t0 = time.monotonic()
        link.close("the node is stopping")
        t.join(1.5)
        assert not t.is_alive() and time.monotonic() - t0 < 0.5
        assert isinstance(out.get("error"), hl.Dropped), out
        assert link.client.losses == 0
    finally:
        _fresh_links()


def test_a_close_while_a_request_holds_the_connection_outside_a_wait_closes_its_socket(
        fake, monkeypatch):
    """The link closes while a request holds the connection between its
    answer and its end — no wait for the close to wake: the request's own
    thread closes the socket as it lets the connection go, and nothing is
    left open."""
    link = _link()
    c = link.client
    read = c._read_response
    answered, release = threading.Event(), threading.Event()

    def holding(wait, on_late=None):
        out = read(wait, on_late)
        answered.set()
        release.wait(5)
        return out
    monkeypatch.setattr(c, "_read_response", holding)

    def ask():
        with link.asking() as hqp:
            hqp.get_status()
    t = threading.Thread(target=ask, daemon=True)
    t.start()
    try:
        assert answered.wait(5)
        link.close("the node is stopping")
    finally:
        release.set()
    t.join(5)
    assert c.socket is None


def test_a_close_ends_a_connect_whose_name_the_resolver_holds(monkeypatch):
    """A name the resolver takes seconds over — a .local HQPlayer switched
    off — holds no node stop: the lookup is a wait the close ends, and the
    request is let go at once, no failure of HQPlayer's."""
    resolving, release = threading.Event(), threading.Event()
    lookup, connect_ex = socket.getaddrinfo, socket.socket.connect_ex

    def held_lookup(host, *args, **kwargs):
        if host == "hqplayer.local":
            resolving.set()
            release.wait(10)
            raise socket.gaierror(socket.EAI_NONAME, "nodename nor servname provided")
        return lookup(host, *args, **kwargs)

    def held_connect(sock, address):
        if address[0] == "hqplayer.local":           # a name looked up inside the connect
            resolving.set()
            release.wait(10)
            return errno.EHOSTUNREACH
        return connect_ex(sock, address)
    monkeypatch.setattr(socket, "getaddrinfo", held_lookup)
    monkeypatch.setattr(socket.socket, "connect_ex", held_connect)
    _fresh_links()
    link = hl.link_for("hqplayer.local", 4321)
    out = {}

    def ask():
        try:
            with link.asking() as c:
                c.get_status()
        except Exception as e:
            out["error"] = e
    t = threading.Thread(target=ask, daemon=True)
    t.start()
    try:
        assert resolving.wait(2)
        t0 = time.monotonic()
        link.close("the node is stopping")
        t.join(1.5)
        assert not t.is_alive() and time.monotonic() - t0 < 0.5
        assert isinstance(out.get("error"), hl.Dropped), out
        assert link.client.losses == 0
    finally:
        release.set()
        _fresh_links()


@pytest.mark.skipif(not os.path.isdir("/dev/fd"), reason="counts the descriptors in /dev/fd")
def test_a_thousand_links_opened_and_closed_leave_no_descriptor_behind(fake):
    """A client's wake channel closes with it: a thousand links that each
    asked HQPlayer once and closed leave the descriptors as they were (the
    fake's own sockets close as the links' do)."""
    def count() -> int:
        return len(os.listdir("/dev/fd"))
    _fresh_links()
    base = count()
    for _ in range(1000):
        link = hl.link_for("127.0.0.1", fake.port)
        with link.asking() as c:
            assert c.get_status() is not None
        link.close("the test opens another")
    assert _wait(lambda: count() <= base, timeout=10), (count(), base)


def test_a_probed_play_that_builds_after_its_answer_is_watched(fake, output, monkeypatch):
    """A Play HQPlayer answers before its build, on the path every owned play
    takes (a replace that probes its first track): the request after it in
    the sequence waits as the Play may make it — watched, up to the build
    limit — so the silence is said as a build, no question meets it, and no
    connection is dropped."""
    mgr, b = output
    _quick_polls(monkeypatch)
    link = _link()
    link.client.timeout = 1.0                           # the plain wait, scaled below the build
    buildings = _seen(mgr, "building")
    fake.build("Play", 3.0)                             # answered, then silent
    connections = link.client.connections
    t0 = time.time()
    mgr.replace_queue([_item(f"E:/Music/C/0{i}.flac", 200 + i) for i in range(1, 4)],
                      play=True, probe_first=True)
    assert _wait(lambda: time.time() > t0 + 4.5, timeout=8)
    assert any(buildings), "the build was never said"
    assert link.client.connections == connections, "a request timed out in the build"


def test_a_short_start_build_after_a_play_answered_first_is_said(fake, output, monkeypatch):
    """The same at the shipped waits: a start of 2.5 s after a Play answered
    first (an SDM start) is a build on the status, not a question's wait."""
    mgr, b = output
    _quick_polls(monkeypatch)
    buildings = _seen(mgr, "building")
    fake.build("Play", 2.5)
    t0 = time.time()
    mgr.replace_queue([_item(f"E:/Music/C/0{i}.flac", 200 + i) for i in range(1, 4)],
                      play=True, probe_first=True)
    assert _wait(lambda: time.time() > t0 + 4.0, timeout=8)
    assert any(buildings), "the build was never said"


def test_a_stop_answered_first_lets_the_replaces_add_wait_its_silence(fake, output):
    """A command behind a Stop waits the Stop's short silence out, up to
    STOP_ANSWER_S: a replace's own add after its Stop — answered at once and
    silent 2 s, longer than the plain wait — goes through."""
    mgr, b = output
    link = _link()
    link.client.timeout = 1.0
    fake.build("Stop", 2.0, answer="first")
    added, _ = mgr.replace_queue([_item(f"E:/Music/D/0{i}.flac", 300 + i) for i in range(1, 4)],
                                 play=False)
    assert added == 3 and b._mirror == "step"


def test_a_command_waiting_behind_a_held_status_past_its_wait_is_refused(fake, monkeypatch):
    """Past STOP_ANSWER_S HQPlayer is not answering, said before the held
    Status gives the connection on: a command waiting for it is refused with
    that line, never sent into the silence. Forced: the held Status's thread
    is held up 0.2 s right after its request."""
    monkeypatch.setattr(hl, "STOP_ANSWER_S", 0.6)
    monkeypatch.setattr(hl.BuildWatch, "after", 0.3)
    link = _link()
    link.client.waits.update(hl._command_waits())
    real = hl.HqpLink._end_scope

    def held_up(self):
        if threading.current_thread().name == "hqp-held-status":
            time.sleep(0.2)
        return real(self)
    monkeypatch.setattr(hl.HqpLink, "_end_scope", held_up)
    fake.build("Stop", 3.0, answer="first")       # answered at once, then silent
    with link.command("transport"):
        assert link.client.stop()
    assert _wait(lambda: [c for _, c in fake.arrivals] == ["Stop", "Status"], timeout=3)
    out = {}

    def press():
        try:
            with link.command("transport"):
                out["ok"] = link.client.play()
        except Exception as e:
            out["error"] = e
    p = threading.Thread(target=press, daemon=True)
    p.start()
    p.join(4)
    assert isinstance(out.get("error"), hl.NotAnswering), out
    assert "Play" not in [c for _, c in fake.arrivals]


def test_the_scan_reads_an_unfinished_build_as_busy(fake, monkeypatch):
    """The picker's scan reads the HQPlayer driven by its link: past a build
    it did not finish it is busy — listed with control and its row — never
    asked into the silence and read as answering discovery only."""
    import asyncio

    import async_upnp_client.search as search
    import hqp_library
    import tls_gen
    from routers import player
    _limit(monkeypatch, 0.5)
    link = _link()
    fake.build("SetFilter", 30, answer="last")
    with link.command("dsp"):
        assert not link.client.set_filter(1)
    assert link.busy() == hl.UNFINISHED

    async def no_search(*a, **k):
        return None

    async def sweep(found, found_hqp):
        found_hqp["127.0.0.1"] = {"name": "fake", "product": "Signalyst HQPlayer Fake"}
    real = hqp_library.resolve_endpoint
    monkeypatch.setattr(hqp_library, "_HASH_TIMEOUT", 0.5)
    monkeypatch.setattr(hqp_library, "resolve_endpoint", lambda host, port: real(host, fake.port))
    monkeypatch.setattr(hl, "link_for", lambda host, port, _real=hl.link_for: _real(host, fake.port))
    monkeypatch.setattr(search, "async_search", no_search)
    monkeypatch.setattr(player, "_unicast_sweep", sweep)
    monkeypatch.setattr(tls_gen, "detect_private_host_ips", lambda: [])
    monkeypatch.setattr(hqp_library, "endpoint_by_address", lambda host, port: {"id": 9})
    monkeypatch.setattr(player, "_configured_hqp", lambda: None)
    monkeypatch.setattr(player, "_dlna_pins", lambda: {})
    monkeypatch.setattr(player, "_hqplayer_entries", lambda: [])
    monkeypatch.setattr(player, "_hqp_discovered", {})
    n = len(fake.arrivals)
    asyncio.run(player._scan_run())
    entry = player._hqp_discovered[("127.0.0.1", 4321)]
    assert entry["control"] is True and entry["endpoint_id"] == 9, entry
    assert fake.arrivals[n:] == []


def test_a_repick_under_another_alias_moves_the_meter_too(fake, listens, monkeypatch):
    """The link dials the alias chosen last, and the meter of the HQPlayer
    driven follows it: a re-pick under another alias changes nothing in
    playback, and the meter stream is opened at the new one."""
    import auth_hmac
    import hqplayer_client
    from playback import hqp_meter
    from playback import manager as manager_mod
    monkeypatch.setattr(auth_hmac, "is_own_address",
                        lambda h: (h or "").lower() in ("nowhere.alias", "127.0.0.1", "localhost"))
    real = hqplayer_client.connect_address
    monkeypatch.setattr(hqplayer_client, "connect_address",
                        lambda h: "::1" if h == "nowhere.alias" else real(h))
    monkeypatch.setattr(settings, "hqplayer_host", "nowhere.alias")
    mgr = PlaybackManager()
    monkeypatch.setattr(manager_mod, "manager", mgr)
    b = _attach(mgr)
    try:
        assert hqp_meter.meter._target == ("nowhere.alias", fake.port + 1)
        router = _picker(monkeypatch, fake)
        _pick(router, hqplayer={"host": "127.0.0.1", "port": fake.port})
        assert mgr.active is b and _link().host == "127.0.0.1"
        assert hqp_meter.meter._target == ("127.0.0.1", fake.port + 1)
    finally:
        mgr.active.shutdown()


def test_a_send_that_failed_ends_no_unfinished_build(fake, monkeypatch):
    """An unfinished build ends with HQPlayer's next answer — never with the
    answer an earlier request of the same thread had: a send that fails
    reads no answer at all."""
    _limit(monkeypatch, 0.5)
    link = _link()
    with link.asking() as c:
        assert c.get_status() is not None            # this thread's last read was answered
    fake.build("SetFilter", 30, answer="last")

    def change():
        with link.command("dsp"):
            link.client.set_filter(1)
    t = threading.Thread(target=change, daemon=True)
    t.start()
    t.join(5)
    assert link.build.state is not None and link.build.state.get("unfinished")

    def failing(client, xml):
        client._io_error = "send failed: [Errno 54] Connection reset by peer"
        client.losses += 1
        client.disconnect()
        return False
    monkeypatch.setattr(HQPlayerClient, "_send_command", failing)
    with link.asking() as c:
        assert c.get_info() is None
    assert link.build.state is not None and link.build.state.get("unfinished")


def test_a_waiter_that_leaves_the_head_of_the_line_wakes_the_next(fake, monkeypatch):
    """The head of the line for the connection refused — a retire — wakes
    the one behind it: the Stop a retiring link owes gets the free
    connection. Forced: the head is held up 0.2 s between being woken and
    looking again."""
    link = _link()
    fake.service["GetInfo"] = 0.5
    late_item = link.item("late_stop", link.accept("switch"))
    link._late_stop = object()          # the link stays retiring: a late Stop is owed

    class Slow(threading.Condition):
        def wait(self, timeout=None):
            r = super().wait(timeout)
            if threading.current_thread().name == "x-question":
                self.release()
                time.sleep(0.2)
                self.acquire()
            return r
    link._conn = Slow()
    out = {}

    def owner():
        with link.asking() as c:
            out["o"] = c.get_info()

    def x():
        try:
            with link.asking() as c:
                out["x"] = c.get_status()
        except Exception as e:
            out["x"] = e

    def late():
        link._local.item = late_item
        link._local.late = True
        try:
            with link.asking() as c:
                out["l"] = c.get_status()
        except Exception as e:
            out["l"] = e
    o = threading.Thread(target=owner, daemon=True)
    o.start()
    assert _wait(lambda: any(c == "GetInfo" for _, c in fake.arrivals), timeout=3)
    tx = threading.Thread(target=x, daemon=True, name="x-question")
    tx.start()
    assert _wait(lambda: len(link._waiting) == 1, timeout=3)
    tl = threading.Thread(target=late, daemon=True, name="hqp-late-stop")
    tl.start()
    assert _wait(lambda: len(link._waiting) == 2, timeout=3)
    assert _wait(lambda: any(w[2] == "out" and w[3] == "GetInfo" for w in fake.wire), timeout=3)
    link.retire("another HQPlayer was chosen")
    tl.join(2)
    assert not tl.is_alive(), "the late Stop's Status never got the free connection"


# -- The mirror's last rows (2026-10-13) -----------------------------------------------

def _followed(b, monkeypatch) -> threading.Event:
    """Set once the reconverge's step (_follow_queue) has run."""
    done = threading.Event()
    follow = b._follow_queue

    def followed(*a, **k):
        try:
            return follow(*a, **k)
        finally:
            done.set()
    monkeypatch.setattr(b, "_follow_queue", followed)
    return done


def test_the_same_file_queued_twice_names_no_slot_for_a_repair(fake, output, monkeypatch):
    """The same file queued twice (an album added again): lost through a
    silence after removals before the playing slot, HQPlayer's entry names
    two slots of the queue — no verdict for a repair, nothing removed from
    under what it is about to play: the mirror stays lost, the play press
    rebuilds."""
    mgr, b = output
    a = "E:/Music/A/01.flac"
    items = ([_item(f"E:/Music/P/0{i}.flac", 50 + i) for i in (1, 2, 3)]
             + [_item(a, 1), _item("E:/Music/A/02.flac", 2), _item("E:/Music/A/03.flac", 3),
                _item(a, 1)])
    added, _ = mgr.replace_queue(items, play=False)
    assert added == 7
    with fake._lock:
        fake.state, fake.track, fake.position = int(PlaybackState.PLAYING), 4, 5.0   # the first A
    assert _wait(lambda: mgr._latest_status.get("track_index") == 4)
    before = list(fake.playlist)
    followed = _followed(b, monkeypatch)
    with mgr._mutate_lock:
        b.lose_mirror("HQPlayer took 0 of 1 appended tracks")      # through a silence
        for _ in range(3):
            assert mgr.remove(1)                                     # the queue's alone: [A, B, C, A]
    assert followed.wait(5)
    assert b._mirror == "lost" and fake.playlist == before
    assert (fake.state, fake.track) == (int(PlaybackState.PLAYING), 4)


def test_a_play_next_on_a_drifted_tail_never_removes_the_entry_read(fake, output, monkeypatch):
    """A playlist that is no mirror is not edited by the queue's places: a
    tail mirror that drifted (an entry added in HQPlayer's own window) takes
    a play-next in the queue alone, and the sound never stops."""
    mgr, b = output
    monkeypatch.setattr(hb, "DRIFT_CHECK_EVERY", 1)
    _tail(mgr, b, fake)          # HQPlayer [01, X09, 02, 03, 04] reads entry 3 = queue slot 2
    with fake._lock:
        fake.playlist.append("file:///Elsewhere/theirs.flac")    # an edit in HQPlayer's window
    assert _wait(lambda: b._mirror == "drift", timeout=5), b._mirror
    fake.commands.clear()
    assert mgr.append([_item("E:/Music/A/07.flac", 7)], position="next") == 1
    assert [c for c, _ in fake.commands if c in ("PlaylistRemove", "PlaylistAdd")] == []
    assert (fake.state, fake.playlist[fake.track - 1]) == (
        int(PlaybackState.PLAYING), "file:///E:/Music/A/02.flac")
    assert [it.track_id for it in mgr.queue.snapshot()][:3] == ["track-1", "track-2", "track-7"]


def test_the_tail_repair_never_removes_the_entry_hqplayer_moved_on_to(fake, output, monkeypatch):
    """The tail is repaired without stopping the sound: HQPlayer may move on
    while the URIs are made (they may transcode or cut), and the repair reads
    its entry again in its own sequence — the entry it moved on to is never
    removed from under it; the repair is due again at its next answer."""
    mgr, b = output                                   # HQPlayer plays entry 1 of [01, 02, 03]
    uri_for = b._uri_for

    def advancing(it):
        with fake._lock:
            if fake.track == 1:
                fake.track, fake.position = 2, 0.0     # 01 ended meanwhile: HQPlayer plays 02
        return uri_for(it)
    followed = _followed(b, monkeypatch)
    with mgr._mutate_lock:
        b.lose_mirror("HQPlayer took 0 of 1 appended tracks")
        assert mgr.remove(2)                           # the queue's alone: [01, 03]
        b._uri_for = advancing
    assert followed.wait(5)
    assert fake.state == int(PlaybackState.PLAYING), (fake.state, fake.track, fake.playlist)
    assert fake.playlist[fake.track - 1] == "file:///E:/Music/A/02.flac"


@pytest.mark.parametrize("press", ["next", "previous"])
def test_next_or_previous_at_the_queues_end_sends_nothing_to_a_playlist_that_is_no_mirror(
        fake, output, press):
    """Next at the queue's last slot, Previous at its first, on a playlist
    that is no mirror: nowhere to go in the queue, and HQPlayer's own Next or
    Previous would move it in a playlist that is not the queue's — nothing is
    sent."""
    mgr, b = output
    with fake._lock:
        fake.track = 1 if press == "previous" else 3
        fake.playlist.insert(0, "file:///Elsewhere/theirs.flac")    # entries of another's
        fake.track += 1
    b.lose_mirror("HQPlayer took 0 of 1 appended tracks", by="refusal")
    want = "track-1" if press == "previous" else "track-3"
    assert _wait(lambda: mgr._latest_status.get("track_id") == want, timeout=5)
    fake.commands.clear()
    assert getattr(b, press)()
    assert [c for c, _ in fake.commands if c in ("Next", "Previous", "Stop", "PlaylistAdd")] == []


# -- The play press: the newest word wins, one rebuild (2026-10-13) -----------------------
# A play press on a playlist that is no mirror hands HQPlayer the queue once,
# off the queue lock, and starts the newest of the play intents made while it
# ran — each numbered as it came — unless a newer one superseded it.

def _album_of(mgr, n):
    """The queue: n tracks of album A, HQPlayer's playlist their mirror."""
    mgr.queue.replace([_item(f"E:/Music/A/{i:02d}.flac", i) for i in range(1, n + 1)])


def _pressing_slowly(mgr, b, fake, n=3, add=0.4):
    """Lost and stopped on a queue of `n`: a play press rebuilds it, each
    add taking `add` s — the window the person's next words land in."""
    if n != 3:
        with mgr._mutate_lock:
            _album_of(mgr, n)
    _stopped(mgr, fake)
    b.lose_mirror("HQPlayer came back with a different playlist (restarted?)", by="refusal")
    fake.service["PlaylistAdd"] = add
    fake.commands.clear()


def _adding(fake):
    assert _wait(lambda: any(c == "PlaylistAdd" for c, _ in fake.commands), timeout=5)


def _rebuilds(fake) -> int:
    return sum(1 for c, a in fake.commands if c == "PlaylistAdd" and a.get("clear") == "1")


def _settled(timeout=10):
    """The press is over, and what it owed went or was dropped."""
    assert _wait(lambda: not any(t.name in ("hqp-press", "hqp-start")
                                 for t in threading.enumerate()), timeout=timeout)
    time.sleep(0.3)


def _quick(fn):
    t = time.monotonic()
    out = fn()
    took = time.monotonic() - t
    assert took < 0.5, f"the route waited {took:.2f} s for the rebuild"
    return out


def test_play_pause_play_during_a_rebuild_is_one_rebuild_and_plays(fake, output, monkeypatch):
    """Play, Pause, Play while the press hands HQPlayer the queue — each
    route answered at once: one rebuild, and the last word, Play, plays."""
    mgr, b = output
    player_router = _routes_on(monkeypatch, mgr)
    _pressing_slowly(mgr, b, fake)
    assert _quick(player_router.play)["ok"]
    _adding(fake)
    assert _quick(player_router.pause) == {"ok": True, "starts": None}
    assert _quick(player_router.play)["starts"]["track_id"] == "track-1"
    _settled()
    assert _rebuilds(fake) == 1
    assert fake.state == int(PlaybackState.PLAYING) and fake.track == 1


def test_play_pause_play_during_a_rebuild_ends_playing(fake, output):
    """The person's last word wins: a Play joining a press after a Pause
    made while it rebuilds is a start of its own — HQPlayer plays."""
    mgr, b = output
    _pressing_slowly(mgr, b, fake, add=0.5)
    assert b.play()
    _adding(fake)
    paused = threading.Thread(target=b.pause, daemon=True)
    paused.start()
    assert _wait(lambda: not paused.is_alive() or _link().lock._waiting, timeout=5)
    assert b.play()
    paused.join(10)
    _settled()
    assert _rebuilds(fake) == 1
    assert fake.state == int(PlaybackState.PLAYING) and fake.track == 1


def test_a_play_route_takes_the_queue_lock_only_to_attach(fake, output, monkeypatch):
    """A play intent on an output attached and answering takes no queue
    lock: it never waits for a queue edit in flight — the press it leads to
    reads the queue once the edit is done."""
    mgr, b = output
    player_router = _routes_on(monkeypatch, mgr)
    _pressing_slowly(mgr, b, fake, add=0.0)
    answered = threading.Event()
    with mgr._mutate_lock:                           # a queue edit still committing
        threading.Thread(target=lambda: (player_router.play(), answered.set()),
                         daemon=True).start()
        assert answered.wait(2)
    _settled()
    assert fake.state == int(PlaybackState.PLAYING) and fake.track == 1


def test_a_tap_during_a_rebuild_starts_the_tapped_track_from_its_start(fake, output, monkeypatch):
    """Play from a spot (track 2 at 0:30), then a tap on the 7th track while
    the press rebuilds: one rebuild, the 7th from 0 s — the second is the
    spot's alone."""
    mgr, b = output
    player_router = _routes_on(monkeypatch, mgr)
    _pressing_slowly(mgr, b, fake, n=8, add=0.2)
    b._set_spot(mgr.queue.item_at(2), 30)
    pressed = _quick(player_router.play)
    _adding(fake)
    tapped = _quick(lambda: player_router.jump(player_router.JumpRequest(index=7)))
    _settled()
    assert _rebuilds(fake) == 1
    assert [(c, a) for c, a in fake.commands if c in ("SelectTrack", "Seek")] == [
        ("SelectTrack", {"index": "7"})]
    assert fake.state == int(PlaybackState.PLAYING) and fake.track == 7
    # each route said at once which track was to start
    assert pressed.get("starts") == {"index": 2, "track_id": "track-2", "position": 30}
    assert tapped.get("starts") == {"index": 7, "track_id": "track-7", "position": 0}


def test_next_twice_during_a_rebuild_moves_the_target_two_on(fake, output, monkeypatch):
    """Next and Next while the press rebuilds count from its target: the
    third track starts, from its start."""
    mgr, b = output
    player_router = _routes_on(monkeypatch, mgr)
    _pressing_slowly(mgr, b, fake, n=5)
    said = [_quick(player_router.play)]
    _adding(fake)
    said += [_quick(player_router.next_track), _quick(player_router.next_track)]
    _settled()
    assert _rebuilds(fake) == 1
    assert fake.state == int(PlaybackState.PLAYING) and fake.track == 3
    assert [(s.get("starts") or {}).get("index") for s in said] == [1, 2, 3]


@pytest.mark.parametrize("plays", [True, False])
def test_a_new_queue_during_a_rebuild_drops_the_old_queues_start(fake, output, plays):
    """A replace while the press rebuilds (a new generation): the old
    queue's start never goes — HQPlayer holds the new queue, playing it
    when the replace plays, else stopped."""
    mgr, b = output
    _pressing_slowly(mgr, b, fake, add=0.3)
    assert b.play()
    _adding(fake)
    album = [_item(f"E:/Music/B/0{i}.flac", 10 + i) for i in range(1, 3)]
    mgr.replace_queue(album, play=plays)
    _settled()
    assert fake.playlist == ["file:///E:/Music/B/01.flac", "file:///E:/Music/B/02.flac"]
    selects = [i for i, (c, _) in enumerate(fake.commands) if c == "SelectTrack"]
    assert not selects, [fake.commands[i] for i in selects]       # the old queue's start
    assert fake.state == int(PlaybackState.PLAYING if plays else PlaybackState.STOPPED)
    assert b._mirror == "step"


def test_the_target_removed_during_a_rebuild_starts_the_next_track(fake, output):
    """The press's target leaves the queue while HQPlayer takes it: the
    start goes from the next track, at its start — on the entry HQPlayer
    holds it at, the playlist (which still holds the removed one) no mirror
    until the next play press."""
    mgr, b = output
    _pressing_slowly(mgr, b, fake, add=0.3)
    b._set_spot(mgr.queue.item_at(2), 30)
    assert b.play()
    _adding(fake)
    assert mgr.remove(2)
    _settled()
    assert _rebuilds(fake) == 1
    assert fake.state == int(PlaybackState.PLAYING)
    assert fake.playlist[fake.track - 1] == "file:///E:/Music/A/03.flac"
    assert "Seek" not in [c for c, _ in fake.commands]
    assert b._mirror == "drift"


def test_queue_edits_during_a_rebuild_never_wait_for_it_and_an_append_follows(fake, output):
    """A queue edit while the press rebuilds is the queue's — answered at
    once, never held behind the rebuild's adds; an append is handed to
    HQPlayer behind what it took, and HQPlayer holds the queue in step."""
    mgr, b = output
    _pressing_slowly(mgr, b, fake, add=0.5)
    assert b.play()
    _adding(fake)
    assert _quick(lambda: mgr.append([_item("E:/Music/A/04.flac", 4)])) == 1
    _settled()
    assert fake.playlist == [f"file:///E:/Music/A/0{i}.flac" for i in range(1, 5)]
    assert b._mirror == "step" and _rebuilds(fake) == 1
    assert fake.state == int(PlaybackState.PLAYING) and fake.track == 1


def test_a_pause_while_a_press_finds_the_mirror_again_stops_hqplayer_there(fake, output):
    """A press that finds HQPlayer's playlist the queue's again (a
    reconverge got there first) rebuilds nothing; a Pause made meanwhile is
    still the last word: HQPlayer stops, at the track the press was to
    start — never playing on."""
    mgr, b = output
    b.lose_mirror("HQPlayer took 0 of 1 appended tracks", by="refusal")
    fake.commands.clear()
    with mgr._mutate_lock:                           # a queue edit still committing
        assert b.select(3)                           # a tap: the press, waiting to begin
        b._in_step()                                 # ...as a reconverge got there first
        assert b.pause()
    _settled()
    assert "PlaylistAdd" not in [c for c, _ in fake.commands]
    assert "Stop" in [c for c, _ in fake.commands]
    assert fake.state == int(PlaybackState.STOPPED)
    assert _wait(lambda: mgr._latest_status.get("resume") == {"index": 3, "position": 0})


def test_a_press_from_the_spot_spends_it(fake, output):
    """A start from the spot spends it — a later Pause, Play resumes
    where it paused, never back at the spot."""
    mgr, b = output
    _pressing_slowly(mgr, b, fake, add=0.0)
    b._set_spot(mgr.queue.item_at(2), 30)
    assert b.play()
    assert _wait(lambda: fake.state == int(PlaybackState.PLAYING) and fake.track == 2, timeout=10)
    _settled()
    assert b._spot is None
    with fake._lock:
        fake.position = 104.0
    assert b.pause()
    n = len(fake.commands)
    assert b.play()
    assert [c for c, _ in fake.commands[n:] if c in ("SelectTrack", "Seek", "Play")] == ["Play"]


def test_a_tap_press_spends_an_older_spot(fake, output):
    """A tap that starts another track spends the spot it read."""
    mgr, b = output
    _pressing_slowly(mgr, b, fake, add=0.0)
    b._set_spot(mgr.queue.item_at(2), 30)
    assert b.select(3)
    assert _wait(lambda: fake.state == int(PlaybackState.PLAYING) and fake.track == 3, timeout=10)
    _settled()
    assert b._spot is None


def test_a_start_dropped_after_a_newer_start_played_puts_no_spot_back(fake, output, monkeypatch):
    """A start left for later (a reorder made while paused, its start owed)
    is dropped by a tap that started first: its spot is not put back —
    HQPlayer plays the tapped track, and a Pause, Play resumes it."""
    from playback.base import ReorderPlan
    mgr, b = output
    with fake._lock:
        fake.state, fake.position = int(PlaybackState.PAUSED), 120.0
    assert _wait(lambda: mgr._latest_status.get("state") == "paused")
    items = mgr.queue.snapshot()
    plan = ReorderPlan(seamless=False, status_idx=1, new_status_idx=2,
                       old_before=[], new_before=[items[1]], old_after=items[1:],
                       new_after=[items[2]], order=[items[1], items[0], items[2]],
                       position=120, resume=True)
    with mgr._mutation():
        b.queue_reorder(plan)
        mgr.queue.reorder(plan.order)
    owed = b._local.start                            # the reorder's start, not sent yet
    b._local.start = None
    assert b.select(3)                               # a tap takes HQPlayer first
    assert _wait(lambda: fake.state == int(PlaybackState.PLAYING) and fake.track == 3)
    b._local.start = owed
    b.queue_start()                                  # superseded: not sent
    assert b._spot is None
    with fake._lock:
        fake.position = 64.0
    assert b.pause()
    n = len(fake.commands)
    assert b.play()
    assert [c for c, _ in fake.commands[n:] if c in ("SelectTrack", "Seek", "Play")] == ["Play"]


def test_a_start_hqplayer_takes_after_it_was_given_up_spends_the_spot(fake, listens, monkeypatch):
    """A tap whose SelectTrack HQPlayer answers only past the build limit is
    given up (a build it did not finish) — then HQPlayer plays the tapped
    track: the start was taken after all, and the spot from before it is
    spent at HQPlayer's first answer. Pause, then Play resumes the tapped
    track, never the old spot."""
    mgr = _album(3)
    b = _attach(mgr)
    try:
        _stopped_at_spot(fake, mgr, monkeypatch)    # queue [3, 2, 1], spot track-2 at 150 s
        assert b._spot is not None and b._spot[0].track_id == "track-2"
        _limit(monkeypatch, 1.0)
        fake.build("SelectTrack", 2.0, answer="last")
        assert not b.select(1)                      # given up: did not finish building
        assert _wait(lambda: fake.state == int(PlaybackState.PLAYING) and fake.track == 1
                     and _link().build.state is None
                     and mgr._latest_status.get("state") == "playing")
        assert _wait(lambda: b._spot is None, timeout=3)
        assert b.pause()
        assert _wait(lambda: mgr._latest_status.get("state") == "paused")
        fake.commands.clear()
        assert b.play()
        assert _wait(lambda: fake.state == int(PlaybackState.PLAYING))
        assert [(c, a) for c, a in fake.commands if c in ("SelectTrack", "Seek", "Play")] == [
            ("Play", {})]
    finally:
        b.shutdown()


def test_a_start_given_up_because_a_newer_word_came_is_superseded_not_unreachable(fake, output):
    """A start that met someone else's build waits for its end; a newer
    intent accepted meanwhile gives it up — work no longer wanted: its
    attempt is interrupted, never "HQPlayer did not answer"."""
    from playback.base import ReorderPlan
    mgr, b = output
    items = mgr.queue.snapshot()
    plan = ReorderPlan(seamless=False, status_idx=1, new_status_idx=2,
                       old_before=[], new_before=[items[1]], old_after=items[1:],
                       new_after=[items[2]], order=[items[1], items[0], items[2]],
                       position=10, resume=True)
    with mgr._mutation():
        b.queue_reorder(plan)
        att = b._attempt
        mgr.queue.reorder(plan.order)
    fake.build("SetFilter", 2.0)
    hb.change_dsp({"filter": 57, "filter1x": 57})    # someone else's build, before the start
    b.queue_start()                                  # meets it: waits in the background
    assert _wait(lambda: any(t.name == "hqp-start" for t in threading.enumerate()), timeout=3)
    _link().accept("transport")                      # a newer word
    assert _wait(lambda: att.verdict is not None, timeout=8)
    assert att.verdict["code"] == "interrupted", (att.verdict, att.error)


def test_a_newer_dsp_change_supersedes_a_resume_that_became_a_press(fake, output, caplog):
    """A DSP change's resume is superseded by a newer DSP change — on a
    playlist that is no mirror too, where the resume is a start of a play
    press: the second change, made while the press takes the queue, leaves
    HQPlayer stopped. Nothing says it resumed while it was only pressed."""
    caplog.set_level("INFO", logger="playback.hqp_backend")
    mgr, b = output
    b.lose_mirror("HQPlayer refused all 3 tracks of a new queue", by="refusal")
    fake.service["PlaylistAdd"] = 0.4
    fake.build("SetFilter", 1.0, answer="last", stops=True)
    fake.commands.clear()
    hb.change_dsp({"filter": 57, "filter1x": 57})    # stops HQPlayer for good
    assert _wait(lambda: any(c == "PlaylistAdd" and a.get("clear") == "1"
                             for c, a in fake.commands), timeout=10)   # its resume: the press
    assert "resumed playback after a DSP change" not in caplog.text
    out = hb.change_dsp({"shaper": 2})               # a newer change, while the press rebuilds
    assert out["ok"], out
    n = len(fake.commands)
    _settled()
    assert [c for c, _ in fake.commands[n:] if c in ("SelectTrack", "Seek", "Play")] == []
    assert fake.state == int(PlaybackState.STOPPED)


def test_a_dsp_change_behind_a_replaces_adds_leaves_the_new_queue_playing(fake, output,
                                                                         monkeypatch):
    """A filter changed at once after an album was played: the change waits
    for the replace's adds, and the entry it would resume is read on its own
    hold — the old queue's is none of the queue's now: nothing resumes, and
    the replace's Play, sent once the change is over, stands."""
    mgr, b = output
    fake.service["PlaylistAdd"] = 0.4
    changed = threading.Event()
    run = hb._DspChange._run

    def ran(self):
        try:
            run(self)
        finally:
            changed.set()
    monkeypatch.setattr(hb._DspChange, "_run", ran)
    queue_start = HqpBackend.queue_start

    def held_start(self):
        if threading.current_thread() is t:          # the replace's Play
            changed.wait(10)
        return queue_start(self)
    monkeypatch.setattr(HqpBackend, "queue_start", held_start)
    album = [_item(f"E:/Music/B/0{i}.flac", 10 + i) for i in range(1, 4)]
    t = threading.Thread(target=lambda: mgr.replace_queue(album, play=True), daemon=True)
    t.start()
    assert _wait(lambda: any(c == "PlaylistAdd" and "B/01" in a.get("uri", "")
                             for c, a in fake.commands), timeout=5)
    # the status a reader would have: the old queue still playing, no tick since the Stop
    mgr._latest_status = {**mgr._latest_status, "state": "playing", "track_index": 1,
                          "position": 42}
    fake.build("SetFilter", 1.0)
    hb.change_dsp({"filter": 57, "filter1x": 57})
    t.join(10)
    assert _wait(lambda: fake.state == int(PlaybackState.PLAYING)
                 and fake.playlist[fake.track - 1] == "file:///E:/Music/B/01.flac", timeout=8)


def test_a_switch_away_never_waits_for_the_owed_stops_teardown(fake, output, monkeypatch):
    """The new output starts once the owed Stop went: the switch waits for
    neither the Stop's answer (HQPlayer tearing its output down) nor, when
    that answer never comes, the Stop's whole wait."""
    mgr, b = output
    _away(monkeypatch, fake)
    fake.service["Stop"] = 3.0                      # answered once the output is torn down
    t0 = time.monotonic()
    told = mgr.activate(None)
    took = time.monotonic() - t0
    mgr.activate("browser")
    assert told is None and took < hl.BUILDING_AFTER_S + 0.4, took
    assert _wait(lambda: "Stop" in [c for _, c in fake.arrivals], timeout=3)


def test_a_switch_away_never_waits_for_the_old_poller(fake, output, monkeypatch):
    """The detach does not wait for the poller: its question waiting for the
    connection behind the owed Stop's teardown is not asked, and the new
    output starts at once."""
    mgr, b = output
    _away(monkeypatch, fake)
    fake.service["Stop"] = 3.0
    release = HqpBackend.release
    released = []

    def release_then_ask(self):
        told = release(self)
        self.poke()                                  # its next question now —
        assert _wait(lambda: _link()._waiting, timeout=3)   # behind the Stop's teardown
        released.append(time.monotonic())
        return told
    monkeypatch.setattr(HqpBackend, "release", release_then_ask)
    mgr.activate(None)
    took = time.monotonic() - released[0]
    mgr.activate("browser")
    assert took < 0.5, took


def test_a_switch_from_a_silent_hqplayer_never_waits_behind_the_pollers_question(
        fake, output, monkeypatch):
    """HQPlayer silent with no build of ours, the poller's Status the
    request it leaves unanswered: the owed Stop's Status waits for the
    connection holding the command lock — the detach does not wait for it,
    and the switch returns within a second, the Stop owed."""
    mgr, b = output
    _away(monkeypatch, fake)
    n = len(fake.arrivals)
    fake.silence(8.0)
    assert _wait(lambda: "Status" in [c for _, c in fake.arrivals[n:]], timeout=3)
    t0 = time.monotonic()
    told = mgr.activate(None)
    took = time.monotonic() - t0
    mgr.activate("browser")
    assert told == hl.STOP_OWED_SILENT
    assert took < hl.BUILDING_AFTER_S + 0.4, took


def test_a_switch_right_after_a_stop_never_waits_out_its_teardown(fake, output, monkeypatch):
    """A person's Stop, then the switch while HQPlayer tears its output down:
    the detach does not wait for that Stop's answer either."""
    mgr, b = output
    _away(monkeypatch, fake)
    fake.service["Stop"] = 3.0
    n = len(fake.arrivals)
    threading.Thread(target=b.stop, daemon=True).start()
    assert _wait(lambda: "Stop" in [c for _, c in fake.arrivals[n:]], timeout=3)
    t0 = time.monotonic()
    mgr.activate(None)
    took = time.monotonic() - t0
    mgr.activate("browser")
    assert took < hl.BUILDING_AFTER_S + 0.4, took


def test_an_address_set_back_to_a_building_hqplayer_supersedes_its_owed_stop(
        fake, fake_b, output, monkeypatch):
    """PUT /api/settings/hqplayer back to the HQPlayer the output left while
    it built: the attach is registered before anything is let go, as the
    picker does — the Stop that switch owed it never goes, even if its build
    ends while the other HQPlayer is let go."""
    mgr, b = output
    settings_router = _picker(monkeypatch, fake)
    fake.build("SetFilter", 1.5)
    began = hb.change_dsp({"filter": 57, "filter1x": 57})["building"]["since"]
    _pick(settings_router, type="hqplayer", hqplayer={"host": "127.0.0.1", "port": fake_b.port})
    assert mgr.active is not b and mgr.active.link.port == fake_b.port
    let_go = HqpBackend.shutdown
    link_a = b.link

    def slow_shutdown(self):
        if self.link.port == fake_b.port:
            assert _wait(lambda: link_a.build.state is None, timeout=10)   # A's build ends here
            time.sleep(0.5)
        let_go(self)
    monkeypatch.setattr(HqpBackend, "shutdown", slow_shutdown)
    settings_router.put_hqplayer_prefs(settings_router.HqplayerPrefs(host="127.0.0.1",
                                                                     port=fake.port))
    try:
        assert mgr.active.link is link_a
        time.sleep(0.5)
        assert [c for ts, c in fake.arrivals if ts >= began and c == "Stop"] == []
    finally:
        mgr.active.shutdown()


def test_a_track_handed_over_then_removed_from_a_lost_mirror_still_owes_the_stop(
        fake, output, monkeypatch):
    """What HQPlayer was given is ours: a slot removed from the queue alone
    (the mirror lost) while HQPlayer plays it is still stopped by a switch
    away — never left playing undriven beside the new output."""
    mgr, b = output
    _away(monkeypatch, fake)
    b.lose_mirror("HQPlayer refused all 2 tracks of an append", by="refusal")
    assert mgr.remove(1)                             # the queue's alone: HQPlayer plays on
    assert fake.state == int(PlaybackState.PLAYING)
    n = len(fake.arrivals)
    mgr.activate(None)
    mgr.activate("browser")
    assert _wait(lambda: "Stop" in [c for _, c in fake.arrivals[n:]], timeout=5)


def test_a_press_behind_a_reconverge_hands_the_queue_over_once(fake, output):
    """HQPlayer answers again with the mirror lost — the reconverge gives
    it the queue back — and the person presses Play meanwhile: HQPlayer is
    given the queue once, then started."""
    mgr, b = output
    _stopped(mgr, fake)
    fake.service["PlaylistAdd"] = 0.3
    fake.commands.clear()
    with mgr._mutate_lock:
        with fake._lock:
            fake.playlist, fake.track = [], 0       # HQPlayer lost what it held
        b.lose_mirror("HQPlayer took 0 of 1 appended tracks")      # through a silence
        assert _wait(lambda: any(t.name == "hqp-reconverge" for t in threading.enumerate()),
                     timeout=5)
    _adding(fake)
    assert b._mirror == "lost"
    assert b.play()                                  # the press, while the queue is handed over
    assert _wait(lambda: fake.state == int(PlaybackState.PLAYING), timeout=10)
    _settled()
    assert _rebuilds(fake) == 1


# -- The rest of the fourth round (2026-10-13) ---------------------------------------------

def test_a_loss_after_the_canarys_check_is_not_undone(fake, output, monkeypatch):
    """The canary's read and its verdict are one compare-and-set: a loss
    landing while it compares (a replace HQPlayer refused whole, its Stop
    gone) is not undone by its stale "in step" — the next Play hands
    HQPlayer the queue."""
    mgr, b = output
    armed = threading.Event()
    first = HqpBackend._first_divergence

    def judging(self, playlist, snapshot):
        if armed.is_set() and threading.current_thread().name == "hqp-status-poller":
            armed.clear()
            monkeypatch.setattr(hb, "DRIFT_CHECK_EVERY", 10 ** 6)   # no second look
            with fake._lock:
                fake.playlist = []
            self.lose_mirror("HQPlayer refused all 3 tracks of a new queue", by="refusal")
        return first(self, playlist, snapshot)
    monkeypatch.setattr(HqpBackend, "_first_divergence", judging)
    judged = threading.Event()
    judge = HqpBackend._judge_mirror

    def judging_done(self, *a, **k):
        try:
            return judge(self, *a, **k)
        finally:
            if not armed.is_set():
                judged.set()
    monkeypatch.setattr(HqpBackend, "_judge_mirror", judging_done)
    monkeypatch.setattr(hb, "DRIFT_CHECK_EVERY", 1)
    armed.set()
    assert _wait(lambda: (b.poke(), judged.is_set())[1], timeout=5)
    assert b._mirror == "lost"
    _stopped(mgr, fake)
    fake.commands.clear()
    assert b.play()
    assert _wait(lambda: any(c == "PlaylistAdd" for c, _ in fake.commands), timeout=5)


def test_reading_tools_go_on_while_hqplayer_refuses_commands_and_say_its_line(fake, output,
                                                                              monkeypatch):
    """HQPlayer answers nothing past a Stop's wait: its commands are refused,
    its questions go on — a reading tool asks and answers, in-process and
    over MCP, and the status tool says the link's own line."""
    import importlib.util
    from tools import definitions
    mgr, b = output
    settings_router = _picker(monkeypatch, fake)
    spec = importlib.util.spec_from_file_location(
        "assistant_server_reads", BACKEND.parent / "mcp" / "assistant_server.py")
    server = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(server)
    monkeypatch.setattr(server, "_output_prefs", settings_router.get_output_prefs)
    monkeypatch.setattr(server, "_backend_get",
                        lambda path, params=None: dict(mgr.latest_status)
                        if path == "/api/player/status" else {})
    monkeypatch.setattr(hl, "STOP_ANSWER_S", 0.6)
    _link().client.waits.update(hl._command_waits())
    fake.build("Stop", 3.0, answer="last")
    assert not b.stop()
    assert _wait(lambda: _link().silent_since is not None, timeout=3)
    said = server.hqplayer_get_status()
    assert not said.startswith("Error") and hl.NOT_ANSWERING in said, said
    read = definitions._h_hqplayer_get_status()
    assert not read.startswith("Error"), read


def test_the_dsp_setters_are_refused_at_once_while_hqplayer_refuses_commands(
        fake, output, monkeypatch):
    """HQPlayer answers nothing past a Stop's wait: a filter, a shaper or a
    matrix profile the assistant sets — in-process and over MCP — is a
    command, refused at once in the link's own line, never a list read
    waited out into the silence and answered with another line."""
    import importlib.util
    from routers import hqplayer as hqp_router
    from tools import definitions
    mgr, b = output
    settings_router = _picker(monkeypatch, fake)
    spec = importlib.util.spec_from_file_location(
        "assistant_server_setters", BACKEND.parent / "mcp" / "assistant_server.py")
    server = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(server)
    monkeypatch.setattr(server, "_output_prefs", settings_router.get_output_prefs)
    monkeypatch.setattr(server, "_backend_get", lambda path, params=None: (
        hqp_router.get_state() if path == "/api/hqplayer/state" else {}))
    monkeypatch.setattr(hl, "STOP_ANSWER_S", 0.6)
    _link().client.waits.update(hl._command_waits())
    fake.build("Stop", 20.0, answer="last")
    assert not b.stop()
    assert _wait(lambda: _link().silent_since is not None, timeout=3)
    t0 = time.monotonic()
    for said in (server.hqplayer_set_filter("sinc-L"), server.hqplayer_set_shaper("NS9"),
                 server.hqplayer_set_matrix_profile("Room"),
                 definitions._h_hqplayer_set_filter("sinc-L"),
                 definitions._h_hqplayer_set_shaper("NS9"),
                 definitions._h_hqplayer_set_matrix_profile("Room")):
        assert hl.NOT_ANSWERING in said, said
    assert time.monotonic() - t0 < 2


def test_a_radio_begun_while_hqplayer_is_stopped_gives_it_the_seed_back(fake, output):
    """Stopped, HQPlayer reads no entry a radio's clear would keep: what the
    clear leaves is no mirror of the seed the queue keeps, and HQPlayer gets
    the queue back at its next answer."""
    mgr, b = output
    _stopped(mgr, fake)
    mgr.clear_for_radio(lambda: None)
    assert [it.track_id for it in mgr.queue.snapshot()] == ["track-1"]   # the seed
    assert _wait(lambda: (b.poke(), fake.playlist == ["file:///E:/Music/A/01.flac"])[1],
                 timeout=5), fake.playlist


# -- Gaps and refusals, again (2026-10-13) --------------------------------------------
# What HQPlayer refused whole is no gap; a "play next" goes where the queue puts
# it, a gap or a tail mirror around it; refusals belong to the HQPlayer, not to one
# attachment; a gap is never the entry HQPlayer reads.

def _answered_after(fake, command: str) -> bool:
    """Has the poller asked HQPlayer again since `command` went — what the
    node meant to send for it is out by then."""
    sent = [c for c, _ in fake.commands]
    return command in sent and "Status" in sent[sent.index(command) + 1:]


def _canary_judged(monkeypatch) -> list:
    """Every verdict the playlist canary gives from here on (its mirror state
    after it)."""
    judged = []
    judge = hb.HqpBackend._judge_mirror

    def counted(self, playlist, status, **kw):
        out = judge(self, playlist, status, **kw)
        judged.append(self._mirror)
        return out
    monkeypatch.setattr(hb.HqpBackend, "_judge_mirror", counted)
    return judged


def test_every_file_refused_on_a_restart_is_no_gap_and_the_next_play_hands_the_queue_over(
        fake, output, monkeypatch):
    """HQPlayer back from a restart refuses every file of the queue it is
    given (its disk not there yet): none of them is a gap — the mirror stays
    lost through the canary's verdicts over its empty playlist — and once the
    files are back the next Play hands the queue over again."""
    mgr, b = output
    monkeypatch.setattr(hl, "CIRCUIT_COOLDOWN_S", 0.0)
    monkeypatch.setattr(hb, "DRIFT_CHECK_EVERY", 1)      # the canary on every answer
    judged = _canary_judged(monkeypatch)
    _stopped(mgr, fake)
    fake.refuse.add("E:/Music/A/")
    n = len(fake.commands)
    fake.restart()                       # back empty and stopped: it is given the queue
    assert _wait(lambda: sum(1 for c, _ in fake.commands[n:] if c == "PlaylistAdd") >= 3,
                 timeout=10)
    seen = len(judged)
    assert _wait(lambda: len(judged) >= seen + 2, timeout=10)   # the canary judged it, twice
    assert b._mirror == "lost" and fake.playlist == []
    assert not any(t.get("gap") for t in mgr.playlist()["tracks"])
    fake.refuse.clear()                  # the files are reachable again
    assert b.play()
    assert _wait(lambda: fake.playlist == [f"file:///E:/Music/A/0{i}.flac" for i in (1, 2, 3)]
                 and fake.state == int(PlaybackState.PLAYING), timeout=10)


def test_a_play_next_at_a_spot_on_a_gap_goes_behind_the_last_entry_held_before_it(
        fake, fake_b, two_libraries):
    """Switched to the HQPlayer that does not hold the slot the queue stood
    on (a file of the other's library), stopped at that spot — a gap here:
    "play next" goes after the last entry HQPlayer holds before it, so its
    playlist stays the queue without its gaps, in order — and Play from the
    spot starts the first track queued next."""
    mgr, settings_router, queue = two_libraries
    mgr._last_slot = 3
    _pick(settings_router, type="hqplayer", hqplayer={"host": "127.0.0.1", "port": fake_b.port})
    b = mgr.active
    assert b._spot_at() == (3, 0)
    assert _wait(lambda: mgr._latest_status.get("track_index") == 3)
    assert fake_b.playlist == ["file:///E:/Music/A/02.flac", "file:///E:/Music/A/04.flac"]
    assert mgr.append([_item("E:/Music/N/01.flac", 31), _item("E:/Music/N/02.flac", 32)],
                      "next") == 2
    assert [it.track_id for it in mgr.queue.snapshot()] == [
        "track-1", "track-2", "track-3", "track-31", "track-32", "track-4"]
    assert fake_b.playlist == ["file:///E:/Music/A/02.flac", "file:///E:/Music/N/01.flac",
                               "file:///E:/Music/N/02.flac", "file:///E:/Music/A/04.flac"]
    assert b._mirror == "step"
    assert b.play()
    assert _wait(lambda: fake_b.state == int(PlaybackState.PLAYING))
    assert fake_b.playlist[fake_b.track - 1] == "file:///E:/Music/N/01.flac"


def test_a_play_next_run_on_a_tail_mirror_keeps_its_order_on_hqplayer(fake, output):
    """A "play next" that lands in pieces (a streamed album as it buffers)
    extends its own run, each piece after the one before it — on a tail
    mirror too: the second lands behind the first on HQPlayer, never at its
    end."""
    mgr, b = output
    _tail(mgr, b, fake)
    assert _wait(lambda: mgr._latest_status.get("track_index") == 2)
    n1, n2 = _item("E:/Music/N/01.flac", 31), _item("E:/Music/N/02.flac", 32)
    assert mgr.append([n1], "next") == 1
    assert mgr.append([n2], "next", after=n1) == 1
    assert [it.track_id for it in mgr.queue.snapshot()] == [
        "track-1", "track-2", "track-31", "track-32", "track-3", "track-4"]
    assert fake.playlist[fake.track - 1:] == [
        "file:///E:/Music/A/02.flac", "file:///E:/Music/N/01.flac",
        "file:///E:/Music/N/02.flac", "file:///E:/Music/A/03.flac",
        "file:///E:/Music/A/04.flac"]
    assert b._mirror == "tail"


def test_a_play_next_hqplayers_playlist_cannot_place_is_the_queues_and_marks_it(
        fake, output):
    """HQPlayer's playlist shorter than the queue it is said to hold: a
    "play next" behind the last slot has no entry to go after — the tracks
    are the queue's alone and the playlist is marked no mirror, never added
    at HQPlayer's end."""
    mgr, b = output
    third = mgr.queue.item_at(3)
    with fake._lock:
        fake.playlist.pop()                          # an edit no canary has seen yet
    held = list(fake.playlist)
    assert mgr.append([_item("E:/Music/N/01.flac", 31)], "next", after=third) == 1
    assert [it.track_id for it in mgr.queue.snapshot()] == [
        "track-1", "track-2", "track-3", "track-31"]
    assert fake.playlist == held
    assert b._mirror == "drift"


def test_a_new_order_while_stopped_at_a_spot_on_a_gap_is_taken(
        fake, fake_b, two_libraries, monkeypatch):
    """A new order made while HQPlayer stands stopped at a spot on a gap —
    no entry of its own to pivot on, no sound to keep: HQPlayer takes the
    queue in its new order, without its gaps, and the mirror stays in step."""
    mgr, settings_router, queue = two_libraries
    player_router = _routes_on(monkeypatch, mgr)
    mgr.queue.replace(queue + [_item("E:/Music/A/05.flac", 5)])
    mgr._last_slot = 3
    _pick(settings_router, type="hqplayer", hqplayer={"host": "127.0.0.1", "port": fake_b.port})
    b = mgr.active
    assert _wait(lambda: mgr._latest_status.get("track_index") == 3)
    assert fake_b.playlist == [f"file:///E:/Music/A/0{i}.flac" for i in (2, 4, 5)]
    order = ["track-1", "track-3", "track-2", "track-5", "track-4"]
    player_router.reorder(player_router.ReorderRequest(order=order))
    assert [it.track_id for it in mgr.queue.snapshot()] == order
    assert fake_b.playlist == [f"file:///E:/Music/A/0{i}.flac" for i in (2, 5, 4)]
    assert b._mirror == "step"


def test_a_reattach_while_hqplayer_plays_a_queue_with_a_refused_gap_is_in_step(fake, output):
    """A file HQPlayer refused among others it took is a gap of that
    HQPlayer — not of one attachment: a backend attached again while it
    plays (a restart, the play gate) finds its playlist to be the queue
    without that gap, and Next stays HQPlayer's own, no rebuild."""
    mgr, b = output
    fake.refuse.add("B/02.flac")
    items = [_item(f"E:/Music/B/0{i}.flac", 10 + i) for i in (1, 2, 3)]
    assert mgr.replace_queue(items, play=True)[0] == 3
    assert _wait(lambda: fake.state == int(PlaybackState.PLAYING) and fake.track == 1)
    assert fake.playlist == ["file:///E:/Music/B/01.flac", "file:///E:/Music/B/03.flac"]
    b.shutdown()                                         # attached again while it plays
    b2 = _attach(mgr)
    try:
        assert b2._mirror == "step"
        assert b2.gap(items[1]) == hb.REFUSED_HERE
        assert _wait(lambda: mgr._latest_status.get("track_index") == 1)
        fake.commands.clear()
        assert b2.next()
        assert _wait(lambda: _answered_after(fake, "Next"))
        assert [c for c, _ in fake.commands if c not in _QUESTIONS] == ["Next"]
    finally:
        b2.shutdown()


def test_a_gap_of_the_same_track_does_not_hold_the_reconverge_back(
        fake, fake_b, two_libraries):
    """The same track at two slots, one a gap here (its copy in the other
    HQPlayer's library), the other a file this HQPlayer plays: a gap is never
    an entry HQPlayer reads, so the entry names one slot and the reconverge
    hands over what was appended while the mirror was lost."""
    mgr, settings_router, queue = two_libraries
    mgr.queue.replace([_held_by("D:/Library/L/01.flac", 1, 5), _item("E:/Music/A/01.flac", 1)])
    _pick(settings_router, type="hqplayer", hqplayer={"host": "127.0.0.1", "port": fake_b.port})
    b = mgr.active
    assert fake_b.playlist == ["file:///E:/Music/A/01.flac"]
    with fake_b._lock:
        fake_b.state, fake_b.track = int(PlaybackState.PLAYING), 1
    assert _wait(lambda: mgr._latest_status.get("track_index") == 2)
    with mgr._mutate_lock:                               # appended while the mirror is lost
        b.lose_mirror("HQPlayer took 0 of 1 appended tracks")
        assert mgr.append([_item("E:/Music/A/02.flac", 2)]) == 1
    assert _wait(lambda: fake_b.playlist == ["file:///E:/Music/A/01.flac",
                                             "file:///E:/Music/A/02.flac"]
                 and b._mirror == "step", timeout=6)


def test_a_tail_mirror_reading_a_file_queued_twice_edits_no_copy_by_a_guess(
        fake, output, monkeypatch):
    """A tail mirror whose entry read is a file queued at two slots: which
    one HQPlayer reads is no verdict, so a removal behind it lands on no copy
    by a guess — it is the queue's, the mirror lost — and the next play press
    gives HQPlayer the queue."""
    mgr, b = output
    mgr.queue.append([_item("E:/Music/A/04.flac", 4)])
    with fake._lock:
        fake.playlist = ["file:///E:/Music/X/91.flac", "file:///E:/Music/X/92.flac",
                         "file:///E:/Music/X/93.flac", "file:///E:/Music/A/01.flac",
                         "file:///E:/Music/A/02.flac", "file:///E:/Music/A/03.flac"]
        fake.track, fake.state, fake.position = 5, int(PlaybackState.PLAYING), 10.0
    b.lose_mirror("HQPlayer took 0 of 1 appended tracks")
    assert _wait(lambda: b._mirror == "tail" and fake.playlist[-1] == "file:///E:/Music/A/04.flac",
                 timeout=5)
    assert mgr.append([_item("E:/Music/A/02.flac", 2)]) == 1          # track-2 queued again
    held = list(fake.playlist)
    assert mgr.remove(3, "track-3")
    assert [it.track_id for it in mgr.queue.snapshot()] == [
        "track-1", "track-2", "track-4", "track-2"]
    assert fake.playlist == held and b._mirror == "lost"
    assert b.select(2)                                                 # a tap: the play press
    assert _wait(lambda: fake.playlist == [f"file:///E:/Music/A/0{i}.flac" for i in (1, 2, 4, 2)]
                 and fake.state == int(PlaybackState.PLAYING) and fake.track == 2, timeout=10)


def test_a_removal_on_a_tail_mirror_reading_no_slot_of_the_queues_marks_it(
        fake, output, monkeypatch):
    """A tail mirror whose HQPlayer has moved on to an entry of another
    controller's: a removal behind the slot it read has no place on HQPlayer —
    it is the queue's, and the mirror is lost, never left a tail it is not."""
    mgr, b = output
    monkeypatch.setattr(hb, "DRIFT_CHECK_EVERY", 10 ** 6)  # no canary between
    _tail(mgr, b, fake)
    with fake._lock:
        fake.playlist.append("file:///Elsewhere/theirs.flac")
        fake.track = len(fake.playlist)
    held = list(fake.playlist)
    assert mgr.remove(4, "track-4")
    assert [it.track_id for it in mgr.queue.snapshot()] == ["track-1", "track-2", "track-3"]
    assert fake.playlist == held and b._mirror == "lost"


def test_a_radio_on_a_tail_mirror_leaves_hqplayer_in_step(fake, output):
    """A radio begun on a tail mirror: HQPlayer's clear keeps the entry it
    reads, the queue the slot it reads — HQPlayer holds the queue, in step,
    and the next Next is HQPlayer's own, no rebuild."""
    mgr, b = output
    _tail(mgr, b, fake)
    assert _wait(lambda: mgr._latest_status.get("track_index") == 2)
    mgr.clear_for_radio(lambda: None)
    assert b._mirror == "step"
    assert mgr.append([_item("E:/Music/R/01.flac", 51)]) == 1
    assert fake.playlist == ["file:///E:/Music/A/02.flac", "file:///E:/Music/R/01.flac"]
    fake.commands.clear()
    assert b.next()
    assert _wait(lambda: _answered_after(fake, "Next"))
    assert [c for c, _ in fake.commands if c not in _QUESTIONS] == ["Next"]


@pytest.mark.parametrize("last", [2, 1])
def test_a_radio_leaves_the_mirror_in_step_only_on_the_slot_the_queue_keeps(
        fake, monkeypatch, last):
    """A radio's clear keeps the entry HQPlayer reads, the queue the slot its
    last status named. The same slot: HQPlayer holds the queue, in step.
    HQPlayer moved on since that status: its playlist is no mirror of the
    queue's seed — the mirror lost, never said in step."""
    from playback import manager as manager_mod
    from playback.base import PlaybackStatus
    monkeypatch.setattr(HqpBackend, "_poll_loop", lambda self: None)    # no status of its own
    mgr = _album(3)
    monkeypatch.setattr(manager_mod, "manager", mgr)
    b = _attach(mgr)
    try:
        with fake._lock:
            fake.state, fake.track = int(PlaybackState.PLAYING), 2      # HQPlayer plays slot 2
        b._last_tick = PlaybackStatus(state="playing", queue_index=last)
        mgr._latest_status["track_index"] = last
        mgr.clear_for_radio(lambda: None)
        assert fake.playlist == ["file:///E:/Music/A/02.flac"]
        assert b._mirror == ("step" if last == 2 else "lost")
    finally:
        b.shutdown()


def test_a_tap_on_a_gap_answers_the_gaps_words(fake, output, monkeypatch):
    """The jump route the queue sheet taps through answers a gap's refusal in
    its words, as every transport route answers a refusal."""
    from playback import substitute
    from routers import player as player_router
    mgr, b = output
    monkeypatch.setattr(player_router, "manager", mgr)
    monkeypatch.setattr(substitute, "native_plays", lambda items, output_id, endpoint_id: [
        {"kind": "unplayable", "reason": "its file left the library"}
        if it.track_id == "track-2" else None for it in items])
    with mgr._mutation():
        b._ways_in()
    answer = player_router.jump(player_router.JumpRequest(index=2))
    assert answer == {"ok": False, "index": 2,
                      "refusal": "it is not on this HQPlayer — its file left the library"}


@pytest.mark.parametrize("position", ["end", "next"])
def test_a_file_hqplayer_refuses_of_an_added_batch_is_a_gap(fake, output, position):
    """An album added — at the end, or to play next — of whose files HQPlayer
    refuses one while it takes the rest: the refused track stays in the
    queue, a gap of this HQPlayer, never removed."""
    mgr, b = output
    fake.refuse.add("B/01.flac")
    assert mgr.append([_item("E:/Music/B/01.flac", 11), _item("E:/Music/B/02.flac", 12)],
                      position) == 2
    tracks = mgr.playlist()["tracks"]
    by_id = {t["track_id"]: t for t in tracks}
    assert set(by_id) == {"track-1", "track-2", "track-3", "track-11", "track-12"}
    assert by_id["track-11"].get("gap") == hb.REFUSED_HERE
    assert "file:///E:/Music/B/02.flac" in fake.playlist
    assert "file:///E:/Music/B/01.flac" not in fake.playlist
    assert b._mirror == "step"


# -- The mirror's races (2026-10-13) ----------------------------------------------------
# A request HQPlayer answers nothing to is carried out after its silence; a plan is
# applied only to the queue it was made on; an entry not ours is judged at once.

def test_a_removal_hqplayer_answers_late_leaves_the_queue_and_loses_the_mirror(fake, output):
    """A removal HQPlayer leaves unanswered within its wait is carried out
    once its silence is over, as an add is: the slot leaves the queue, the
    mirror is lost — never kept in step over a playlist about to change — and
    HQPlayer, answering again, is the queue's mirror once more."""
    mgr, b = output
    assert mgr.append([_item("E:/Music/A/04.flac", 4)]) == 1
    losses = b.mirror_losses
    _link().client.timeout = 0.5                       # a command's wait, shortened
    fake.service["PlaylistRemove"] = 1.0               # busy: carried out late, all the same
    try:
        assert mgr.remove(2, "track-2")
    finally:
        del fake.service["PlaylistRemove"]
        _link().client.timeout = hl.COMMAND_WAIT_S
    assert [it.track_id for it in mgr.queue.snapshot()] == ["track-1", "track-3", "track-4"]
    assert b.mirror_losses == losses + 1
    assert _wait(lambda: fake.playlist == [f"file:///E:/Music/A/0{i}.flac" for i in (1, 3, 4)]
                 and b._mirror == "step", timeout=8)


def test_a_radios_clear_hqplayer_answers_late_starts_the_radio_and_loses_the_mirror(
        fake, output, monkeypatch):
    """The radio's PlaylistClear left unanswered within its wait is carried
    out once HQPlayer's silence is over: the radio goes on — the queue keeps
    the seed alone — the mirror lost, and HQPlayer answering again holds the
    queue."""
    mgr, b = output
    losses = b.mirror_losses
    monkeypatch.setattr(hl, "STOP_ANSWER_S", 0.6)      # a short silence's wait, shortened
    _link().client.waits.update(hl._command_waits())
    fake.service["PlaylistClear"] = 1.0
    try:
        gen = mgr.clear_for_radio(lambda: None)
    finally:
        del fake.service["PlaylistClear"]
    assert gen == mgr.queue.generation
    assert [it.track_id for it in mgr.queue.snapshot()] == ["track-1"]
    assert b.mirror_losses == losses + 1
    assert _wait(lambda: fake.playlist == ["file:///E:/Music/A/01.flac"]
                 and b._mirror == "step", timeout=8)


def test_a_new_order_planned_before_a_fillers_append_landed_is_refused_whole(
        fake, output, monkeypatch):
    """The reorder route plans on the queue it reads; a filler's append that
    lands before the plan is applied makes it a plan of a queue that is gone:
    refused whole (409), the queue and HQPlayer in the order they hold, the
    mirror in step."""
    from fastapi import HTTPException
    mgr, b = output
    player_router = _routes_on(monkeypatch, mgr)
    apply = mgr.apply_reorder

    def a_filler_first(plan):
        assert mgr.append([_item("E:/Music/A/04.flac", 4)]) == 1     # lands meanwhile
        return apply(plan)
    monkeypatch.setattr(mgr, "apply_reorder", a_filler_first)
    with pytest.raises(HTTPException) as refused:
        player_router.reorder(player_router.ReorderRequest(order=["track-1", "track-3", "track-2"]))
    assert refused.value.status_code == 409
    assert [it.track_id for it in mgr.queue.snapshot()] == [
        "track-1", "track-2", "track-3", "track-4"]
    assert fake.playlist == [f"file:///E:/Music/A/0{i}.flac" for i in (1, 2, 3, 4)]
    assert b._mirror == "step"


def test_an_entry_of_another_controllers_read_while_in_step_is_judged_at_its_answer(
        fake, output, monkeypatch):
    """Another controller adds an entry of its own and plays it while the
    mirror is in step: the tick that reads it has the canary judge the
    playlist then and there — drifted — so a tap right after hands HQPlayer
    the queue and plays its track, never selects into the other playlist."""
    mgr, b = output
    monkeypatch.setattr(hb, "DRIFT_CHECK_EVERY", 10 ** 6)        # no canary of its cadence
    with fake._lock:
        fake.playlist.append("file:///Elsewhere/theirs.flac")
        fake.track = len(fake.playlist)
    assert _wait(lambda: b._mirror == "drift", timeout=5)
    assert b.select(2)
    assert _wait(lambda: fake.playlist == [f"file:///E:/Music/A/0{i}.flac" for i in (1, 2, 3)]
                 and fake.state == int(PlaybackState.PLAYING) and fake.track == 2, timeout=10)


def test_an_entry_not_ours_is_judged_at_once_only_once(fake, output, monkeypatch):
    """The canary reads the playlist the queue's although HQPlayer reads an
    entry no slot names: it is not asked again at every answer — once per
    entry and queue version, then at its own cadence."""
    mgr, b = output
    monkeypatch.setattr(hb, "DRIFT_CHECK_EVERY", 10 ** 6)
    judged = []
    monkeypatch.setattr(hb.HqpBackend, "_judge_mirror",
                        lambda self, playlist, status, **kw: judged.append(status.uri) or True)
    with fake._lock:
        fake.playlist.append("file:///Elsewhere/theirs.flac")
        fake.track = len(fake.playlist)
    n = len(fake.commands)
    assert _wait(lambda: sum(1 for c, _ in fake.commands[n:] if c == "Status") >= 6, timeout=10)
    assert judged == ["file:///Elsewhere/theirs.flac"]


# -- The play press, again (2026-10-13) --------------------------------------------------
# Only a live press takes the words made meanwhile; a new queue cuts its hand-over
# short; a Seek moves the newest word's second; the rebuild's spot, the judge of a
# start given up on, what is ours at a switch, the gate and the detach around it.

def _b_album():
    return [_item(f"E:/Music/B/0{i}.flac", 10 + i) for i in range(1, 4)]


def _start_sent(fake, n=0) -> list:
    return [(c, a) for c, a in fake.commands[n:] if c in ("SelectTrack", "Seek", "Play")]


def test_a_play_while_a_new_queue_loads_over_a_press_plays_that_queue(fake, output, monkeypatch):
    """Play presses a press (HQPlayer takes the queue again); the person picks
    an album and, while it loads, presses Play again: the newest word is Play
    on the new queue — it joins the album's start, and HQPlayer plays it."""
    mgr, b = output
    player_router = _routes_on(monkeypatch, mgr)
    _pressing_slowly(mgr, b, fake, add=0.4)
    assert _quick(player_router.play)["ok"]
    _adding(fake)
    loading = threading.Thread(target=lambda: mgr.replace_queue(_b_album(), play=True),
                               daemon=True)
    loading.start()
    assert _wait(lambda: any(c == "PlaylistAdd" and "B/01" in a.get("uri", "")
                             for c, a in fake.commands), timeout=10)
    said = player_router.play()
    assert said["starts"] == {"index": 1, "track_id": "track-11", "position": 0}
    loading.join(15)
    _settled()
    assert fake.playlist == [f"file:///E:/Music/B/0{i}.flac" for i in range(1, 4)]
    assert fake.state == int(PlaybackState.PLAYING) and fake.track == 1


def test_a_pause_after_a_radio_overtook_a_pending_press_pauses(fake, output):
    """A tap is a press still waiting to begin when a radio starts (a new
    queue, the seed playing on): the press leads nowhere now, and the
    person's Pause is no word to it — it pauses HQPlayer."""
    mgr, b = output
    b.lose_mirror("HQPlayer refused all 3 tracks of a new queue", by="refusal")
    with mgr._mutate_lock:                            # the radio start holds the queue lock
        assert b.select(3)                            # the tap: a press, waiting to begin
        mgr.clear_for_radio(lambda: None)             # the radio: a new queue
        assert b.pause()
    _settled()
    assert fake.state == int(PlaybackState.PAUSED)
    assert "Pause" in [c for c, _ in fake.commands]


def test_a_play_during_a_reorders_rebuild_leaves_no_spot_behind_what_plays(
        fake, output, monkeypatch):
    """Track 1 plays at 0:42; a new order needs a rebuild (track 1 moves);
    the person presses Play while HQPlayer takes the new order: held, it
    asks for the rebuild's own start, sent as the Play's — a start taken
    spends the spot, none is put back under what plays, and a later Pause
    and Play only pause and resume."""
    mgr, b = output
    player_router = _routes_on(monkeypatch, mgr)
    fake.service["PlaylistAdd"] = 0.4
    fake.commands.clear()
    reorder = threading.Thread(target=lambda: player_router.reorder(
        player_router.ReorderRequest(order=["track-2", "track-1", "track-3"])), daemon=True)
    reorder.start()
    assert _wait(lambda: any(c == "PlaylistAdd" and a.get("clear") == "1"
                             for c, a in fake.commands), timeout=5)
    assert b.play()
    reorder.join(10)
    _settled()
    assert fake.state == int(PlaybackState.PLAYING)
    assert b._spot is None
    assert _wait(lambda: mgr._latest_status.get("state") == "playing")
    assert b.pause()
    assert _wait(lambda: mgr._latest_status.get("state") == "paused")
    n = len(fake.commands)
    assert b.play()
    assert _start_sent(fake, n) == [("Play", {})]


def test_a_new_queue_cuts_a_press_hand_over_short(fake, output):
    """A replace while a press hands HQPlayer eight tracks: the new queue
    is the newest word as it is asked — the old queue's remaining adds do not
    go, and the album route waits for its own."""
    mgr, b = output
    _pressing_slowly(mgr, b, fake, n=8, add=0.4)
    assert b.play()
    _adding(fake)
    n = len(fake.commands)
    t0 = time.monotonic()
    mgr.replace_queue(_b_album(), play=True)
    took = time.monotonic() - t0
    _settled()
    old = [a.get("uri") for c, a in fake.commands[n:] if c == "PlaylistAdd" and "/A/" in a.get("uri", "")]
    assert len(old) <= 1, old
    assert took < 0.4 * 3 + 1.0, took
    assert fake.state == int(PlaybackState.PLAYING)
    assert fake.playlist == [f"file:///E:/Music/B/0{i}.flac" for i in range(1, 4)]


def test_a_replace_that_does_not_commit_leaves_no_mirror_in_step(fake, output):
    """A replace whose session rotation fails (nothing commits) hands
    HQPlayer its album over a press's hand-over: the press's end does not
    call that playlist the queue's — nothing starts on it, and a tap then
    plays the queue's track, never one of the album the queue does not
    hold."""
    mgr, b = output
    _pressing_slowly(mgr, b, fake, add=0.8)
    adds = sum(1 for _, c in fake.arrivals if c == "PlaylistAdd")
    assert b.play()
    # Its last add with HQPlayer, not answered yet: the replace numbered now
    # drops nothing of the hand-over, and the press ends after the replace
    assert _wait(lambda: sum(1 for _, c in fake.arrivals if c == "PlaylistAdd") == adds + 3,
                 timeout=5)

    def gone():
        raise RuntimeError("the database is away")
    failed = []

    def replace():
        try:
            mgr.replace_queue(_b_album(), play=True, archive=gone)
        except RuntimeError as e:
            failed.append(e)
    replacing = threading.Thread(target=replace, daemon=True)
    replacing.start()
    replacing.join(15)
    assert failed
    _settled()
    assert [it.track_id for it in mgr.queue.snapshot()] == ["track-1", "track-2", "track-3"]
    assert _start_sent(fake) == []
    assert b.select(2)
    assert _wait(lambda: fake.state == int(PlaybackState.PLAYING)
                 and fake.playlist == [f"file:///E:/Music/A/0{i}.flac" for i in range(1, 4)]
                 and fake.track == 2, timeout=10)


def _presses_held(monkeypatch, b, begin: threading.Event) -> None:
    """Every play press of `b` begun from here on waits for `begin` before
    its first step: registered, its hand-over not begun, while the test goes
    on."""
    once = b._press_once

    def held(press):
        begin.wait(20)
        once(press)
    monkeypatch.setattr(b, "_press_once", held)


def test_an_overtaken_press_ends_leaving_the_next_one_to_run(fake, output, monkeypatch):
    """A replace that does not commit overtakes a press near the end of its
    hand-over; the person taps a track once the replace has failed, before
    the overtaken press has ended: the tap is a press of its own. The
    overtaken press's end leaves the tap's press running — it says what it
    starts, and HQPlayer plays the tapped track of the queue."""
    mgr, b = output
    _pressing_slowly(mgr, b, fake, add=0.8)
    adds = sum(1 for _, c in fake.arrivals if c == "PlaylistAdd")
    assert b.play()
    first = next(t for t in threading.enumerate() if t.name == "hqp-press")
    begin, ending = threading.Event(), threading.Event()
    _presses_held(monkeypatch, b, begin)             # the tap's press waits to begin
    assert _wait(lambda: sum(1 for _, c in fake.arrivals if c == "PlaylistAdd") == adds + 3,
                 timeout=5)
    overtaken = b._pressed
    end = b._press_end

    def held_end(press, *args):
        if press is overtaken:
            ending.wait(20)                          # the overtaken press's end, held
        end(press, *args)
    monkeypatch.setattr(b, "_press_end", held_end)

    def gone():
        raise RuntimeError("the database is away")
    failed = []

    def replace():
        try:
            mgr.replace_queue(_b_album(), play=True, archive=gone)
        except RuntimeError as e:
            failed.append(e)
    replacing = threading.Thread(target=replace, daemon=True)
    try:
        replacing.start()
        replacing.join(15)
        assert failed
        assert b._live(overtaken) is None and b._pressed is overtaken
        assert b.select(2)                           # the tap: a press of its own
        ending.set()
        first.join(10)
        assert not first.is_alive()
        assert b.pressing() == {"index": 2, "track_id": "track-2", "position": 0}
    finally:
        ending.set()
        begin.set()
    _settled()
    assert fake.state == int(PlaybackState.PLAYING)
    assert fake.playlist == [f"file:///E:/Music/A/0{i}.flac" for i in range(1, 4)]
    assert fake.track == 2


def test_a_start_given_up_and_taken_is_judged_on_a_tick_that_names_its_slot(
        fake, listens, monkeypatch):
    """A tap HQPlayer takes only after the node gave it up on, its first
    answer after it the opening tick (playing, no track named yet): that
    tick gives no verdict, the next one does — the old spot is spent, and a
    Pause then a Play only resume."""
    mgr = _album(3)
    b = _attach(mgr)
    try:
        _stopped_at_spot(fake, mgr, monkeypatch)    # queue [3, 2, 1], the spot track-2 at 150 s
        assert b._spot is not None and b._spot[0].track_id == "track-2"
        _limit(monkeypatch, 1.0)
        fake.build("SelectTrack", 2.0, answer="last")
        fake.opening = 1                            # the first playing tick names no track
        assert not b.select(1)                      # given up on: past the build limit
        assert _wait(lambda: fake.state == int(PlaybackState.PLAYING) and fake.track == 1
                     and _link().build.state is None)
        assert _wait(lambda: b._spot is None, timeout=5)
        assert _wait(lambda: mgr._latest_status.get("state") == "playing")
        assert b.pause()
        assert _wait(lambda: mgr._latest_status.get("state") == "paused")
        fake.commands.clear()
        assert b.play()
        assert _wait(lambda: fake.state == int(PlaybackState.PLAYING))
        assert _start_sent(fake) == [("Play", {})]
    finally:
        b.shutdown()


def test_a_file_once_handed_and_played_by_another_controller_is_not_stopped_at_a_switch(
        fake, output, monkeypatch):
    """The queue was album A, then album B; another controller plays A/02 —
    a file this node handed HQPlayer before the new queue, not an entry of
    what it holds now: a switch away stops nothing of another controller's."""
    mgr, b = output
    _away(monkeypatch, fake)
    mgr.replace_queue([_item(f"E:/Music/B/0{i}.flac", 10 + i) for i in range(1, 3)], play=False)
    with fake._lock:
        fake.playlist = ["file:///E:/Music/A/02.flac"]
        fake.track, fake.state, fake.position = 1, int(PlaybackState.PLAYING), 5.0
    assert _wait(lambda: mgr._latest_status.get("state") == "playing"
                 and mgr._latest_status.get("track_index") == 0, timeout=5)
    n = len(fake.arrivals)
    mgr.activate(None)
    mgr.activate("browser")
    assert _wait(lambda: not any(t.name == "hqp-late-stop" for t in threading.enumerate()))
    assert "Stop" not in [c for _, c in fake.arrivals[n:]]


def test_a_hold_begun_during_a_press_hand_over_waits_for_none_of_it(fake, output, monkeypatch):
    """A benchmark run begins while a press hands HQPlayer eight tracks: the
    link is lent first, so the hand-over's next add is refused — the hold
    waits for the request on its way, never the whole hand-over."""
    mgr, b = output
    _picker(monkeypatch, fake)
    _pressing_slowly(mgr, b, fake, n=8, add=0.4)
    assert b.play()
    _adding(fake)
    n = len(fake.commands)
    t0 = time.monotonic()
    try:
        with mgr.hold("benchmark", "The HQPlayer benchmark is running"):
            took = time.monotonic() - t0
            adds = [a.get("uri") for c, a in fake.commands[n:] if c == "PlaylistAdd"]
    finally:
        if mgr.active is not None and mgr.active is not b:
            mgr.active.shutdown()
    _settled()
    assert took < 1.2 and len(adds) <= 1, (took, adds)


def test_a_second_intent_during_a_press_teardown_answers_at_once(fake, output, monkeypatch):
    """A tap on a lost mirror is a press whose hand-over opens with a Stop
    HQPlayer answers once its output is down (2.5 s here): a Next made
    meanwhile is answered at once — the play gate asks no question that
    would wait behind that teardown."""
    mgr, b = output
    player_router = _routes_on(monkeypatch, mgr)
    b.lose_mirror("HQPlayer refused all 3 tracks of a new queue", by="refusal")
    fake.service["Stop"] = 2.5
    n = len(fake.arrivals)
    assert _quick(lambda: player_router.jump(player_router.JumpRequest(index=2)))["ok"]
    assert _wait(lambda: "Stop" in [c for _, c in fake.arrivals[n:]], timeout=5)
    assert _quick(player_router.next_track)["ok"]
    _settled()
    assert fake.state == int(PlaybackState.PLAYING) and fake.track == 3


def test_a_switch_waits_for_no_stop_waiting_its_turn_on_the_connection(
        fake, output, monkeypatch):
    """HQPlayer silent, the poller's question on the connection; the person
    presses Stop — waiting its turn — and switches the output: the switch
    goes on at once, the Stop's turn a teardown to come."""
    mgr, b = output
    _away(monkeypatch, fake)
    n = len(fake.arrivals)
    fake.silence(8.0)
    assert _wait(lambda: "Status" in [c for _, c in fake.arrivals[n:]], timeout=3)
    stopped = {}

    def stop():
        try:
            stopped["ok"] = b.stop()
        except hl.Dropped as e:
            stopped["dropped"] = e
    stopper = threading.Thread(target=stop, daemon=True)
    stopper.start()
    assert _wait(lambda: _link().lock.owner is not None and _link()._waiting, timeout=3)
    t0 = time.monotonic()
    mgr.activate(None)
    took = time.monotonic() - t0
    mgr.activate("browser")
    assert took < hl.BUILDING_AFTER_S + 0.4, took
    _link().close("the test is over")
    stopper.join(5)
    assert "dropped" in stopped, stopped        # its turn never came: the link let go


def test_a_seek_during_a_rebuild_moves_the_newest_starts_second(fake, output, monkeypatch):
    """Play, then a Seek to 60 s while the press hands the queue over: the
    Seek decides nothing about playing — it moves the newest start's second,
    answered at once; HQPlayer plays from 60 s."""
    mgr, b = output
    player_router = _routes_on(monkeypatch, mgr)
    _pressing_slowly(mgr, b, fake, add=0.4)
    assert _quick(player_router.play)["ok"]
    _adding(fake)
    assert _quick(lambda: player_router.seek(player_router.SeekRequest(position=60)))["ok"]
    _settled()
    assert _rebuilds(fake) == 1
    assert fake.state == int(PlaybackState.PLAYING) and fake.track == 1
    assert ("Seek", {"position": "60"}) in _start_sent(fake)


def test_a_seek_after_a_pause_during_a_rebuild_moves_the_spot_alone(fake, output):
    """Play, Pause, then a Seek to 60 s while the press hands the queue over:
    silence — the Pause stays the newest word — the spot the press leaves at
    60 s, and the next Play starts there."""
    mgr, b = output
    _pressing_slowly(mgr, b, fake, add=0.4)
    assert b.play()
    _adding(fake)
    assert b.pause()
    assert b.seek(60)
    _settled()
    assert fake.state != int(PlaybackState.PLAYING)
    assert b._spot is not None and (b._spot[0].track_id, b._spot[1]) == ("track-1", 60)
    n = len(fake.commands)
    assert b.play()
    assert _wait(lambda: fake.state == int(PlaybackState.PLAYING))
    assert ("Seek", {"position": "60"}) in _start_sent(fake, n)


def test_a_seek_after_a_tap_during_a_rebuild_starts_the_tapped_track_at_its_second(
        fake, output, monkeypatch):
    """Play, a tap on the 7th track, then a Seek to 30 s while the press
    hands the queue over: the 7th track from 30 s."""
    mgr, b = output
    player_router = _routes_on(monkeypatch, mgr)
    _pressing_slowly(mgr, b, fake, n=8, add=0.2)
    assert _quick(player_router.play)["ok"]
    _adding(fake)
    assert _quick(lambda: player_router.jump(player_router.JumpRequest(index=7)))["ok"]
    assert _quick(lambda: player_router.seek(player_router.SeekRequest(position=30)))["ok"]
    _settled()
    assert _rebuilds(fake) == 1
    assert fake.state == int(PlaybackState.PLAYING) and fake.track == 7
    assert _start_sent(fake)[-3:-1] == [("SelectTrack", {"index": "7"}), ("Seek", {"position": "30"})]


def _radio_over_a_pending_press(mgr, b, fake):
    """Stopped, its playlist no mirror: a tap is a press still waiting to
    begin (the radio start holds the queue lock — the caller's, held on
    return) when a radio starts — a new queue, the seed kept, a track of it
    queued. The clear leaves HQPlayer no entry: the press leads nowhere, and
    the radio's queue is no one's on HQPlayer."""
    _stopped(mgr, fake)
    b.lose_mirror("HQPlayer refused all 3 tracks of a new queue", by="refusal")
    assert b.select(3)
    mgr.clear_for_radio(lambda: None)
    assert mgr.append([_item("E:/Music/R/01.flac", 51)]) == 1


def test_a_play_after_a_radio_overtook_a_pending_press_is_a_press_of_its_own(
        fake, output, monkeypatch):
    """A Play made once a radio overtook a pending press is a press of its
    own: HQPlayer takes the radio's queue and plays the seed, where it
    stopped."""
    mgr, b = output
    player_router = _routes_on(monkeypatch, mgr)
    with mgr._mutate_lock:
        _radio_over_a_pending_press(mgr, b, fake)
        assert player_router.play()["starts"]["track_id"] == "track-1"
    _settled()
    assert fake.playlist == ["file:///E:/Music/A/01.flac", "file:///E:/Music/R/01.flac"]
    assert fake.state == int(PlaybackState.PLAYING) and fake.track == 1


def test_a_word_after_a_radio_overtook_a_pending_press_is_no_word_to_it(
        fake, output, monkeypatch):
    """Once a radio overtook a pending press, a Pause names no track the
    press would start, and a tap is a press of its own — HQPlayer takes the
    radio's queue and plays the tapped track."""
    mgr, b = output
    player_router = _routes_on(monkeypatch, mgr)
    with mgr._mutate_lock:
        _radio_over_a_pending_press(mgr, b, fake)
        assert "starts" not in player_router.pause()
        tapped = player_router.jump(player_router.JumpRequest(index=2))
        assert tapped["starts"]["track_id"] == "track-51"
    _settled()
    assert fake.state == int(PlaybackState.PLAYING)
    assert fake.playlist[fake.track - 1] == "file:///E:/Music/R/01.flac"


def test_a_play_after_a_new_queue_overtook_a_pending_press_plays_it_as_handed_over(
        fake, output, monkeypatch):
    """A tap is a press not yet begun when the person loads an album without
    playing it: HQPlayer takes the album, in step. A Play made then is a
    Play of its own and plays the album at once, as it was handed over —
    never a press, which would name a track to start and wait behind the one
    the album overtook."""
    mgr, b = output
    player_router = _routes_on(monkeypatch, mgr)
    _stopped(mgr, fake)
    b.lose_mirror("HQPlayer refused all 3 tracks of a new queue", by="refusal")
    begin = threading.Event()
    _presses_held(monkeypatch, b, begin)
    try:
        assert b.select(3)
        mgr.replace_queue(_b_album(), play=False)
        n = len(fake.commands)
        assert "starts" not in player_router.play()
        assert _wait(lambda: fake.state == int(PlaybackState.PLAYING))
        assert fake.playlist == [f"file:///E:/Music/B/0{i}.flac" for i in range(1, 4)]
        assert fake.track == 1
        assert "PlaylistAdd" not in [c for c, _ in fake.commands[n:]]
    finally:
        begin.set()
    _settled()


def test_a_resume_after_a_new_queue_overtook_a_pending_press_goes_in_place(
        fake, output, monkeypatch):
    """A tap is a press not yet begun when the person plays an album:
    HQPlayer takes it, in step, and plays. A DSP change that stops HQPlayer
    for good resumes the album in place, at once — never as a press, which
    would wait behind the one the album overtook."""
    mgr, b = output
    _stopped(mgr, fake)
    b.lose_mirror("HQPlayer refused all 3 tracks of a new queue", by="refusal")
    begin = threading.Event()
    _presses_held(monkeypatch, b, begin)
    try:
        assert b.select(3)
        mgr.replace_queue(_b_album(), play=True)
        assert _wait(lambda: mgr._latest_status.get("track_id") == "track-11"
                     and mgr._latest_status.get("state") == "playing")
        fake.build("SetFilter", 1.0, answer="last", stops=True)
        n = len(fake.commands)
        hb.change_dsp({"filter": 57, "filter1x": 57})
        assert _wait(lambda: any(c == "SelectTrack" for c, _ in fake.commands[n:]), timeout=10)
        assert _wait(lambda: fake.state == int(PlaybackState.PLAYING))
        assert fake.playlist[fake.track - 1] == "file:///E:/Music/B/01.flac"
        assert "PlaylistAdd" not in [c for c, _ in fake.commands[n:]]
    finally:
        begin.set()
    _settled()


def test_hqplayer_gets_a_radios_queue_back_past_the_press_it_overtook(fake, output,
                                                                       monkeypatch):
    """A tap is a press not yet begun when a radio starts on a stopped
    HQPlayer: the clear leaves it no entry, and it gets the radio's queue
    back at its next answer — the press the radio overtook hands it none,
    and is not waited for."""
    mgr, b = output
    _stopped(mgr, fake)
    b.lose_mirror("HQPlayer refused all 3 tracks of a new queue", by="refusal")
    begin = threading.Event()
    _presses_held(monkeypatch, b, begin)
    try:
        assert b.select(3)
        mgr.clear_for_radio(lambda: None)
        assert _wait(lambda: fake.playlist == ["file:///E:/Music/A/01.flac"]
                     and b._mirror == "step", timeout=10)
    finally:
        begin.set()
    _settled()


# -- a person's words while a queue mutation is in flight -------------------------
# A replace, a reorder, a re-bind's rebuild and the radio's clear hold the words
# made while they hand HQPlayer its playlist, as a play press does: each word
# names a track, never a slot, translated against what HQPlayer is handed; the
# newest goes once the queue is committed — a Play joins the start the mutation
# owes.

def _slow_adds(fake, add=0.4) -> int:
    fake.service["PlaylistAdd"] = add
    return len(fake.commands)


def _handing_over(fake, n):
    """The mutation's first add is with HQPlayer: its hand-over is under way."""
    assert _wait(lambda: any(c == "PlaylistAdd" and a.get("clear") == "1"
                             for c, a in fake.commands[n:]), timeout=5)


def _loading(mgr, items, **kw) -> threading.Thread:
    loading = threading.Thread(target=lambda: mgr.replace_queue(items, **kw), daemon=True)
    loading.start()
    return loading


def _new_with_3_at_2():
    """A new queue holding track 3 of the old one at its second slot."""
    return [_item("E:/Music/A/02.flac", 2), _item("E:/Music/A/03.flac", 3),
            _item("E:/Music/B/04.flac", 14)]


def test_a_tap_during_a_replace_plays_the_tapped_track(fake, output, monkeypatch):
    """The queue sheet still shows the old queue while a new one is handed
    over, and the person taps its third row: the tap names that track, not
    the row — the new queue holds it at its second slot, and HQPlayer plays
    it there from its start once the new queue is committed."""
    mgr, b = output
    player_router = _routes_on(monkeypatch, mgr)
    n = _slow_adds(fake, add=0.9)                     # the play gate asks HQPlayer nothing
    loading = _loading(mgr, _new_with_3_at_2(), play=True)
    _handing_over(fake, n)
    tapped = _quick(lambda: player_router.jump(
        player_router.JumpRequest(index=3, track_id="track-3")))
    assert tapped["starts"] == {"index": 2, "track_id": "track-3", "position": 0}
    loading.join(10)
    _settled()
    assert fake.playlist == ["file:///E:/Music/A/02.flac", "file:///E:/Music/A/03.flac",
                             "file:///E:/Music/B/04.flac"]
    assert fake.state == int(PlaybackState.PLAYING) and fake.track == 2
    assert _wait(lambda: mgr._latest_status.get("track_id") == "track-3")


def test_a_tap_on_a_track_a_replace_takes_away_is_refused(fake, output, monkeypatch):
    """A tap during a replace on a track the new queue does not hold names
    nothing HQPlayer is handed: it is refused in words, and the album plays
    from its start as asked."""
    mgr, b = output
    player_router = _routes_on(monkeypatch, mgr)
    n = _slow_adds(fake)
    loading = _loading(mgr, _b_album(), play=True)
    _handing_over(fake, n)
    tapped = _quick(lambda: player_router.jump(
        player_router.JumpRequest(index=2, track_id="track-2")))
    assert tapped["ok"] is False and "not in the queue being loaded" in tapped["refusal"]
    loading.join(10)
    _settled()
    assert fake.state == int(PlaybackState.PLAYING) and fake.track == 1
    assert fake.playlist[0] == "file:///E:/Music/B/01.flac"


def test_a_play_during_a_reorders_rebuild_plays_the_track_where_it_was(fake, output,
                                                                       monkeypatch):
    """Track 1 plays at 0:42; a new order needs a rebuild (track 1 moves to
    the second slot), and the person presses Play while HQPlayer takes it:
    the Play joins the start the rebuild owes — track 1, from 0:42."""
    mgr, b = output
    player_router = _routes_on(monkeypatch, mgr)
    n = _slow_adds(fake)
    reorder = threading.Thread(target=lambda: player_router.reorder(
        player_router.ReorderRequest(order=["track-2", "track-1", "track-3"])), daemon=True)
    reorder.start()
    _handing_over(fake, n)
    said = _quick(player_router.play)
    assert said["starts"]["index"] == 2 and said["starts"]["track_id"] == "track-1"
    assert said["starts"]["position"] >= 42
    reorder.join(10)
    _settled()
    assert fake.playlist == ["file:///E:/Music/A/02.flac", "file:///E:/Music/A/01.flac",
                             "file:///E:/Music/A/03.flac"]
    assert fake.state == int(PlaybackState.PLAYING) and fake.track == 2
    sent = _start_sent(fake, n)
    assert sent[0] == ("SelectTrack", {"index": "2"})
    assert sent[1][0] == "Seek" and int(sent[1][1]["position"]) >= 42


def test_a_play_during_a_replace_joins_the_start_it_owes(fake, output, monkeypatch):
    """A Play while HQPlayer takes an album asks again for the start the
    replace owes: that start goes as the replace sends it — Play on the
    album, no slot selected — numbered as the person's Play."""
    mgr, b = output
    player_router = _routes_on(monkeypatch, mgr)
    n = _slow_adds(fake)
    loading = _loading(mgr, _b_album(), play=True)
    _handing_over(fake, n)
    assert _quick(player_router.play)["starts"]["track_id"] == "track-11"
    loading.join(10)
    _settled()
    assert _start_sent(fake, n) == [("Play", {})]
    assert fake.state == int(PlaybackState.PLAYING) and fake.track == 1


def test_next_during_a_replace_plays_the_new_queues_second_track(fake, output):
    """The person plays an album and presses Next while HQPlayer takes it:
    Next counts from the start the replace owes — the album's first track —
    and HQPlayer plays its second."""
    mgr, b = output
    n = _slow_adds(fake)
    loading = _loading(mgr, _b_album(), play=True)
    _handing_over(fake, n)
    assert _quick(b.next)
    loading.join(10)
    _settled()
    assert fake.playlist == [f"file:///E:/Music/B/0{i}.flac" for i in range(1, 4)]
    assert fake.state == int(PlaybackState.PLAYING) and fake.track == 2


def test_a_seek_during_a_replace_starts_the_album_at_that_second(fake, output):
    """A Seek while HQPlayer takes an album changes the second of the start
    the replace owes: the album's first track plays from 1:00."""
    mgr, b = output
    n = _slow_adds(fake)
    loading = _loading(mgr, _b_album(), play=True)
    _handing_over(fake, n)
    assert _quick(lambda: b.seek(60))
    loading.join(10)
    _settled()
    assert fake.state == int(PlaybackState.PLAYING) and fake.track == 1
    assert fake.playlist[0] == "file:///E:/Music/B/01.flac"
    assert ("Seek", {"position": "60"}) in _start_sent(fake, n)


def test_a_pause_during_a_replace_leaves_the_album_to_the_next_play(fake, output):
    """A Pause while HQPlayer takes an album drops the start the replace
    owes: HQPlayer stays stopped with the album, and the next Play starts its
    first track."""
    mgr, b = output
    n = _slow_adds(fake)
    loading = _loading(mgr, _b_album(), play=True)
    _handing_over(fake, n)
    assert _quick(b.pause)
    loading.join(10)
    _settled()
    assert fake.state == int(PlaybackState.STOPPED)
    assert b.play()
    assert _wait(lambda: fake.state == int(PlaybackState.PLAYING))
    assert fake.playlist[fake.track - 1] == "file:///E:/Music/B/01.flac"


def test_a_pause_after_a_tap_during_a_replace_leaves_the_spot_at_the_tapped_track(
        fake, output, monkeypatch):
    """While HQPlayer takes a new queue the person taps a track of it, then
    pauses: nothing starts — HQPlayer stays stopped with the new queue, the
    spot at the tapped track — and the next Play starts that track."""
    mgr, b = output
    player_router = _routes_on(monkeypatch, mgr)
    n = _slow_adds(fake)
    loading = _loading(mgr, _new_with_3_at_2(), play=True)
    _handing_over(fake, n)
    assert _quick(lambda: player_router.jump(
        player_router.JumpRequest(index=3, track_id="track-3")))["ok"]
    assert _quick(b.pause)
    loading.join(10)
    _settled()
    assert fake.state == int(PlaybackState.STOPPED)
    assert b.play()
    assert _wait(lambda: fake.state == int(PlaybackState.PLAYING))
    assert fake.playlist[fake.track - 1] == "file:///E:/Music/A/03.flac"


def test_a_tap_while_a_new_order_waits_its_turn_plays_the_tapped_track(fake, output,
                                                                       monkeypatch):
    """A new order is asked while HQPlayer still answers an earlier command
    (a slow volume change): the rebuild is numbered as it is asked, so a tap
    made while it waits its turn on the connection is the newer word — the
    tapped track plays, not the rebuild's resume of the one that played."""
    mgr, b = output
    player_router = _routes_on(monkeypatch, mgr)
    fake.service["Volume"] = 1.5
    n, came = len(fake.commands), len(fake.arrivals)
    volume = threading.Thread(target=lambda: b.set_volume(-10), daemon=True)
    volume.start()
    assert _wait(lambda: "Volume" in [c for _, c in fake.arrivals[came:]], timeout=5)
    reorder = threading.Thread(target=lambda: player_router.reorder(
        player_router.ReorderRequest(order=["track-2", "track-1", "track-3"])), daemon=True)
    reorder.start()
    assert _wait(lambda: b._inflight is not None, timeout=5)
    assert _quick(lambda: player_router.jump(
        player_router.JumpRequest(index=2, track_id="track-2")))["starts"]["index"] == 1
    volume.join(10)
    reorder.join(10)
    _settled()
    assert fake.playlist[0] == "file:///E:/Music/A/02.flac"
    assert fake.state == int(PlaybackState.PLAYING) and fake.track == 1
    assert ("SelectTrack", {"index": "1"}) in _start_sent(fake, n)


def test_a_play_during_a_rebinds_rebuild_resumes_the_track_where_it_was(fake, output):
    """Track 2 plays; the file of slot 1 left the library and its re-bound
    slot needs a rebuild around the playing one. A Play while HQPlayer takes
    it joins the start the rebuild owes: track 2 plays on at its second."""
    mgr, b = output
    with fake._lock:
        fake.track, fake.position = 2, 80.0
    assert _wait(lambda: mgr._latest_status.get("track_index") == 2
                 and mgr._latest_status.get("position", 0) >= 81)
    n = _slow_adds(fake)

    def rebind():
        with mgr._mutate_lock, b.mutation():          # as rebind_files does
            _move(mgr, 1, "E:/Music/B/01.flac", 10)
            b.queue_changed("rebind")
        b.queue_start()
    rebinding = threading.Thread(target=rebind, daemon=True)
    rebinding.start()
    _handing_over(fake, n)
    assert _quick(b.play)
    rebinding.join(10)
    _settled()
    assert fake.playlist == ["file:///E:/Music/B/01.flac", "file:///E:/Music/A/02.flac",
                             "file:///E:/Music/A/03.flac"]
    assert fake.state == int(PlaybackState.PLAYING) and fake.track == 2
    sent = _start_sent(fake, n)
    assert sent[0] == ("SelectTrack", {"index": "2"})
    assert sent[1][0] == "Seek" and int(sent[1][1]["position"]) >= 80


def test_a_tap_during_a_seamless_reorder_plays_the_tapped_track_at_its_new_slot(
        fake, output, monkeypatch):
    """Track 1 plays; the person swaps tracks 2 and 3 (the playing one stays:
    HQPlayer takes the new order around it, never stopping) and taps the
    second row while it does: the tap names track 2, which the new order
    holds third — HQPlayer plays it there."""
    mgr, b = output
    player_router = _routes_on(monkeypatch, mgr)
    fake.service["PlaylistRemove"] = 0.4
    n = len(fake.commands)
    reorder = threading.Thread(target=lambda: player_router.reorder(
        player_router.ReorderRequest(order=["track-1", "track-3", "track-2"])), daemon=True)
    reorder.start()
    assert _wait(lambda: any(c == "PlaylistRemove" for c, _ in fake.commands[n:]), timeout=5)
    tapped = _quick(lambda: player_router.jump(
        player_router.JumpRequest(index=2, track_id="track-2")))
    assert tapped["starts"] == {"index": 3, "track_id": "track-2", "position": 0}
    reorder.join(10)
    _settled()
    assert fake.playlist == ["file:///E:/Music/A/01.flac", "file:///E:/Music/A/03.flac",
                             "file:///E:/Music/A/02.flac"]
    assert fake.state == int(PlaybackState.PLAYING) and fake.track == 3


def test_a_pause_during_the_radios_clear_pauses_the_seed(fake, output):
    """The radio starts while track 1 plays (HQPlayer keeps it, clearing the
    rest); a Pause made while the clear is on its way pauses the seed once
    the radio's queue stands."""
    mgr, b = output
    fake.service["PlaylistClear"] = 0.5
    n, came = len(fake.commands), len(fake.arrivals)
    radio = threading.Thread(target=lambda: mgr.clear_for_radio(lambda: None), daemon=True)
    radio.start()
    assert _wait(lambda: "PlaylistClear" in [c for _, c in fake.arrivals[came:]], timeout=5)
    assert _quick(b.pause)
    radio.join(10)
    _settled()
    assert fake.state == int(PlaybackState.PAUSED)
    assert fake.playlist == ["file:///E:/Music/A/01.flac"]
    assert [c for c, _ in fake.commands[n:] if c in ("Pause", "PlaylistClear")] == [
        "PlaylistClear", "Pause"]


def test_a_tap_held_by_a_replace_that_does_not_commit_is_said_with_its_refusal(
        fake, output, monkeypatch):
    """A replace whose session rotation fails commits nothing: a tap made
    while it handed HQPlayer the album names a track only that album holds —
    nothing is started on HQPlayer for it, and the replace's refusal says
    so: the route answers it with the refusal's words."""
    mgr, b = output
    player_router = _routes_on(monkeypatch, mgr)
    n = _slow_adds(fake)
    failed = []

    def gone():
        raise RuntimeError("the database is away")

    def replace():
        try:
            mgr.replace_queue(_b_album(), play=True, archive=gone)
        except RuntimeError as e:
            failed.append(e)
    loading = threading.Thread(target=replace, daemon=True)
    loading.start()
    _handing_over(fake, n)
    assert _quick(lambda: player_router.jump(
        player_router.JumpRequest(index=2, track_id="track-12")))["ok"]
    loading.join(10)
    assert failed
    _settled()
    assert _start_sent(fake, n) == []
    assert [it.track_id for it in mgr.queue.snapshot()] == ["track-1", "track-2", "track-3"]
    said = getattr(failed[0], "__notes__", [])
    assert said == ["Song 12, played meanwhile, is not in the queue — not played"], said
    from routers import player as player_router_mod
    assert player_router_mod._output_error(failed[0]).detail == (
        "the database is away — Song 12, played meanwhile, is not in the queue — not played")


# -- The intent clock: the order of the asks -------------------------------------
#
# An intent is numbered as it is asked and holds once its sequence has the
# command lock; a new queue and a rebuild supersede from their ask and
# withdraw that when the lock refuses them.


def test_a_number_that_holds_late_lowers_no_newer_one():
    """Two Plays asked in a row, the first's thread switched out between its
    ask and the lock: the second holds first. The first, holding after it,
    does not make the newer one unasked — a start made between the two stays
    superseded."""
    link = hl.HqpLink(("clock", 0), "127.0.0.1", 9)
    first = link.reserve()
    start = link.item("start", link.reserve())
    second = link.reserve()
    link.publish("transport", second)
    link.publish("transport", first)
    assert link.superseded(start) == "a newer request came first"


def test_a_new_queue_the_lock_refuses_is_withdrawn():
    """A new queue supersedes from its ask; withdrawn — the lock refused it —
    it supersedes nothing: a start older than it goes again."""
    link = hl.HqpLink(("clock", 0), "127.0.0.1", 9)
    start = link.item("start", link.accept("transport"))
    n = link.ask_early("queue")
    assert link.superseded(start) == "another queue plays"
    link.withdraw(n)
    assert link.superseded(start) is None


@pytest.mark.parametrize("edit", ["radio", "new order", "queue put together"])
def test_a_queue_edit_refused_while_hqplayer_builds_supersedes_nothing(fake, output,
                                                                      monkeypatch, edit):
    """A DSP change stops HQPlayer for a build and owes a resume of the track
    that played. The person starts a radio, drags the playing track (a new
    order HQPlayer takes afresh) or puts a queue together while it builds:
    refused at once, the queue as it was — and the change's resume goes all
    the same once the build is done."""
    from fastapi import HTTPException
    mgr, b = output
    player_router = _routes_on(monkeypatch, mgr)
    fake.build("SetFilter", 2.0, answer="last", stops=True)
    assert hb.change_dsp({"filter": 57, "filter1x": 57})["building"] is not None
    asked = {"radio": lambda: mgr.clear_for_radio(lambda: None),
             "new order": lambda: player_router.reorder(
                 player_router.ReorderRequest(order=["track-2", "track-1", "track-3"])),
             "queue put together": lambda: mgr.replace_queue(
                 [_item(f"E:/Music/B/0{i}.flac", 10 + i) for i in range(1, 4)], play=False)}
    n = len(fake.commands)
    with pytest.raises((hl.Building, HTTPException)) as refused:
        asked[edit]()
    if isinstance(refused.value, HTTPException):
        assert refused.value.status_code == 409
    assert [it.track_id for it in mgr.queue.snapshot()] == ["track-1", "track-2", "track-3"]
    # HQPlayer plays on while it builds, and stops once it is done
    assert _wait(lambda: ("SelectTrack", {"index": "1"}) in _start_sent(fake, n), timeout=8), \
        "the change's resume never went"
    assert _wait(lambda: fake.state == int(PlaybackState.PLAYING) and fake.track == 1)


def test_a_play_asked_before_a_new_order_leaves_the_order_its_resume(fake, output,
                                                                     monkeypatch):
    """HQPlayer answers a Pause slowly — the command lock held. The person
    presses Play (it waits its turn), then drags the playing track down: a
    new order HQPlayer takes afresh, asked after the Play and waiting behind
    it. The Play is the older word: the new order's resume of the track
    that played still goes, and HQPlayer plays on."""
    mgr, b = output
    player_router = _routes_on(monkeypatch, mgr)
    fake.service["Pause"] = 1.5
    pausing = threading.Thread(target=b.pause, daemon=True)
    pausing.start()
    assert _wait(lambda: any(c == "Pause" for _, c in fake.arrivals), timeout=3)
    playing = threading.Thread(target=b.play, daemon=True)
    playing.start()
    assert _wait(lambda: len(_link().lock._waiting) == 1, timeout=3)      # its turn
    reordering = threading.Thread(target=lambda: player_router.reorder(
        player_router.ReorderRequest(order=["track-2", "track-1", "track-3"])), daemon=True)
    reordering.start()
    assert _wait(lambda: len(_link().lock._waiting) == 2, timeout=3)      # behind the Play
    pausing.join(5)
    playing.join(5)
    reordering.join(15)
    _settled()
    assert fake.playlist == [f"file:///E:/Music/A/0{i}.flac" for i in (2, 1, 3)]
    assert fake.state == int(PlaybackState.PLAYING) and fake.track == 2, (
        fake.state, fake.track, _start_sent(fake))


def test_a_tap_while_the_reconverge_makes_its_uris_plays_the_tapped_track(fake, output,
                                                                        monkeypatch):
    """HQPlayer back stopped with an empty playlist: the reconverge gives it
    the queue afresh, making each slot's URI first (a cut, a transcode:
    seconds). A tap made then is held and answered with what it starts — and
    that track plays: the rebuild was numbered before it."""
    mgr, b = output
    player_router = _routes_on(monkeypatch, mgr)
    _stopped(mgr, fake)
    with fake._lock:
        fake.playlist, fake.track = [], 0                 # restarted: nothing held
    entered, go = threading.Event(), threading.Event()
    uri_for = b._uri_for

    def slow(item):
        if threading.current_thread().name == "hqp-reconverge" and not entered.is_set():
            entered.set()
            go.wait(10)
        return uri_for(item)
    monkeypatch.setattr(b, "_uri_for", slow)
    b.lose_mirror("HQPlayer came back with a different playlist (restarted?)")
    assert entered.wait(10), "the reconverge never made its URIs"
    tapped = _quick(lambda: player_router.jump(
        player_router.JumpRequest(index=3, track_id="track-3")))
    go.set()
    assert _wait(lambda: not any(t.name == "hqp-reconverge" for t in threading.enumerate()))
    _settled()
    assert fake.playlist == [f"file:///E:/Music/A/0{i}.flac" for i in (1, 2, 3)]
    assert fake.state == int(PlaybackState.PLAYING) and fake.track == 3, (
        tapped, fake.state, fake.track, _start_sent(fake))


@pytest.mark.parametrize("word", ["tap", "pause"])
def test_a_word_as_the_replace_hands_over_is_newer_than_it(fake, output, monkeypatch, word):
    """A replace that plays has begun holding words and is about to hand
    HQPlayer the new queue (its thread switched out right there): a word
    made then is newer than the replace — a tap plays the tapped track, a
    Pause leaves HQPlayer stopped instead of the album starting."""
    mgr, b = output
    player_router = _routes_on(monkeypatch, mgr)
    entered, go = threading.Event(), threading.Event()
    afresh = b._afresh

    def switched_out(uris, intent, **kw):
        if intent == "queue" and not entered.is_set():
            entered.set()
            go.wait(10)
        return afresh(uris, intent, **kw)
    monkeypatch.setattr(b, "_afresh", switched_out)
    n = len(fake.commands)
    loading = _loading(mgr, _new_with_3_at_2(), play=True)
    assert entered.wait(10) and b._inflight is not None
    if word == "tap":
        said = _quick(lambda: player_router.jump(
            player_router.JumpRequest(index=3, track_id="track-3")))
        assert said["starts"] == {"index": 2, "track_id": "track-3", "position": 0}, said
    else:
        assert _quick(b.pause)
    go.set()
    loading.join(10)
    _settled()
    if word == "tap":
        assert fake.state == int(PlaybackState.PLAYING) and fake.track == 2, (
            fake.state, fake.track, _start_sent(fake, n))
    else:
        assert fake.state == int(PlaybackState.STOPPED), _start_sent(fake, n)


# -- A held word's own outcome ------------------------------------------------------
#
# The words a mutation held go once the queue stands — committed or not — as
# the same words would go then, each with an outcome of its own.


@pytest.mark.parametrize("busy", ["silent", "building"])
def test_a_word_held_during_the_radios_clear_answers_for_itself(fake, output, monkeypatch,
                                                                 busy):
    """The person presses Pause while the radio's clear is on its way, and
    HQPlayer is then busy — the clear left unanswered past its wait, or a
    filter change made right after that builds. The radio's queue is
    committed: the Pause's outcome is the Pause's, never the radio start's —
    the radio goes on, and a Pause that met a build goes once it is done."""
    mgr, b = output
    if busy == "silent":
        monkeypatch.setattr(hl, "STOP_ANSWER_S", 0.6)
        _link().client.waits.update(hl._command_waits())
        fake.service["PlaylistClear"] = 1.5
    else:
        fake.service["PlaylistClear"] = 0.6
        fake.build("SetFilter", 2.0)
    out = {}

    def radio():
        try:
            out["gen"] = mgr.clear_for_radio(lambda: None)
        except Exception as e:
            out["error"] = e
    gen = mgr.queue.generation
    started = threading.Thread(target=radio, daemon=True)
    came = len(fake.arrivals)
    started.start()
    assert _wait(lambda: b._inflight is not None
                 and "PlaylistClear" in [c for _, c in fake.arrivals[came:]], timeout=5)
    assert b.pause()
    if busy == "building":
        threading.Thread(target=lambda: hb.change_dsp({"filter": 57}), daemon=True).start()
    started.join(10)
    fake.service.pop("PlaylistClear", None)
    assert "error" not in out, out
    assert mgr.queue.generation != gen
    if busy == "building":
        assert _wait(lambda: fake.state == int(PlaybackState.PAUSED), timeout=8), fake.state


def test_a_held_pause_a_newer_word_overtook_does_not_go(fake, output, monkeypatch):
    """A Pause held while HQPlayer takes a new order around the playing
    track; the order commits, and the person presses Play before the held
    Pause is carried out: the Play is the newer word — HQPlayer plays on."""
    mgr, b = output
    player_router = _routes_on(monkeypatch, mgr)
    fake.service["PlaylistRemove"] = 0.4
    start = b.queue_start

    def play_first():
        if threading.current_thread() is reordering:
            assert b.play()                     # the newer word, made right after the commit
        return start()
    monkeypatch.setattr(b, "queue_start", play_first)
    n = len(fake.commands)
    reordering = threading.Thread(target=lambda: player_router.reorder(
        player_router.ReorderRequest(order=["track-1", "track-3", "track-2"])), daemon=True)
    reordering.start()
    assert _wait(lambda: any(c == "PlaylistRemove" for c, _ in fake.commands[n:]), timeout=5)
    assert _quick(b.pause)
    reordering.join(10)
    _settled()
    assert fake.state == int(PlaybackState.PLAYING), [c for c, _ in fake.commands[n:]]
    assert "Pause" not in [c for c, _ in fake.commands[n:]]


def test_a_tap_read_while_a_mutation_ends_is_held_by_it(fake, output, monkeypatch):
    """A tap reads the words a replace holds and resolves its track while
    the replace ends: the replace's end waits for it, and carries it out —
    the tapped track plays, never a word lost to a mutation that ended."""
    mgr, b = output
    player_router = _routes_on(monkeypatch, mgr)
    n = _slow_adds(fake)
    entered, go = threading.Event(), threading.Event()
    gap = b.gap
    tapper = {}

    def gap_held(item):
        if threading.current_thread() is tapper.get("t") and not entered.is_set():
            entered.set()
            go.wait(10)                         # the tap's thread switched out here
        return gap(item)
    monkeypatch.setattr(b, "gap", gap_held)
    loading = _loading(mgr, _b_album(), play=True)
    _handing_over(fake, n)
    said = {}
    t = threading.Thread(target=lambda: said.update(player_router.jump(
        player_router.JumpRequest(index=2, track_id="track-12"))), daemon=True)
    tapper["t"] = t
    t.start()
    assert entered.wait(10)
    loading.join(1.0)                           # the replace's end, if nothing holds it back
    go.set()
    t.join(10)
    loading.join(10)
    _settled()
    assert said.get("ok"), said
    assert fake.state == int(PlaybackState.PLAYING) and fake.track == 2, _start_sent(fake, n)


def test_pause_seek_play_during_a_press_plays_from_the_seek(fake, output):
    """Play, Pause, a Seek to 60 s and Play again, all while the press hands
    the queue over: the Seek gave the halted word its second, and the Play
    after it starts there."""
    mgr, b = output
    _pressing_slowly(mgr, b, fake, add=0.4)
    assert b.play()
    _adding(fake)
    assert b.pause()
    assert b.seek(60)
    assert b.play()
    _settled()
    assert fake.state == int(PlaybackState.PLAYING) and fake.track == 1
    assert ("Seek", {"position": "60"}) in _start_sent(fake), _start_sent(fake)


def test_pause_seek_play_during_a_replace_plays_from_the_seek(fake, output):
    """The same three words while a replace hands HQPlayer an album: its
    first track starts at 60 s."""
    mgr, b = output
    n = _slow_adds(fake)
    loading = _loading(mgr, _b_album(), play=True)
    _handing_over(fake, n)
    assert _quick(b.pause)
    assert _quick(lambda: b.seek(60))
    assert _quick(b.play)
    loading.join(10)
    _settled()
    assert fake.state == int(PlaybackState.PLAYING) and fake.track == 1
    assert ("Seek", {"position": "60"}) in _start_sent(fake, n), _start_sent(fake, n)


def test_pause_seek_play_during_the_radios_clear_keeps_the_seek(fake, output):
    """The same while the radio's clear is on its way, the seed playing on:
    the seed plays at 60 s once the radio's queue stands."""
    mgr, b = output
    fake.service["PlaylistClear"] = 0.6
    n, came = len(fake.commands), len(fake.arrivals)
    radio = threading.Thread(target=lambda: mgr.clear_for_radio(lambda: None), daemon=True)
    radio.start()
    assert _wait(lambda: "PlaylistClear" in [c for _, c in fake.arrivals[came:]], timeout=5)
    assert _quick(b.pause)
    assert _quick(lambda: b.seek(60))
    assert _quick(b.play)
    radio.join(10)
    _settled()
    assert fake.state == int(PlaybackState.PLAYING)
    assert ("Seek", {"position": "60"}) in [(c, a) for c, a in fake.commands[n:] if c == "Seek"], \
        [c for c, _ in fake.commands[n:]]


def test_a_tap_while_the_reconverge_reads_plays_the_tapped_track(fake, output, monkeypatch):
    """HQPlayer back stopped with an empty playlist: the reconverge reads it
    under the queue lock before it holds any word, and a tap made then is a
    play press. The reconverge yields to it — the press hands HQPlayer the
    queue — and the tapped track plays."""
    mgr, b = output
    player_router = _routes_on(monkeypatch, mgr)
    _stopped(mgr, fake)
    with fake._lock:
        fake.playlist, fake.track = [], 0
    entered, go = threading.Event(), threading.Event()
    get_playlist = HQPlayerClient.get_playlist

    def held_read(client, *a, **kw):
        out = get_playlist(client, *a, **kw)
        if threading.current_thread().name == "hqp-reconverge" and not entered.is_set():
            entered.set()
            go.wait(10)
        return out
    monkeypatch.setattr(HQPlayerClient, "get_playlist", held_read)
    b.lose_mirror("HQPlayer came back with a different playlist (restarted?)")
    assert entered.wait(10)
    said = _quick(lambda: player_router.jump(player_router.JumpRequest(index=3, track_id="track-3")))
    assert said["starts"] == {"index": 3, "track_id": "track-3", "position": 0}, said
    go.set()
    assert _wait(lambda: not any(t.name == "hqp-reconverge" for t in threading.enumerate()))
    _settled()
    assert fake.playlist == [f"file:///E:/Music/A/0{i}.flac" for i in (1, 2, 3)]
    assert fake.state == int(PlaybackState.PLAYING) and fake.track == 3, (
        fake.state, fake.track, _start_sent(fake))


def test_a_tap_during_a_new_order_behind_a_tail_mirror_plays_the_tapped_track(
        fake, output, monkeypatch):
    """A tail mirror — HQPlayer reads track 2 at its third entry, an entry of
    its own before it — takes a new order behind the playing slot; the
    person taps track 4 meanwhile. HQPlayer's entries are no slots of the
    queue's: the tap goes as it would with nothing in flight, a play press —
    HQPlayer takes the queue and plays track 4, never its own entry 3."""
    mgr, b = output
    player_router = _routes_on(monkeypatch, mgr)
    _tail(mgr, b, fake)
    assert _wait(lambda: mgr._latest_status.get("track_index") == 2, timeout=5)
    fake.service["PlaylistRemove"] = 0.4
    n = len(fake.commands)
    reorder = threading.Thread(target=lambda: player_router.reorder(player_router.ReorderRequest(
        order=["track-1", "track-2", "track-4", "track-3"])), daemon=True)
    reorder.start()
    assert _wait(lambda: any(c == "PlaylistRemove" for c, _ in fake.commands[n:]), timeout=5)
    assert b._inflight is not None
    assert _quick(lambda: player_router.jump(
        player_router.JumpRequest(index=4, track_id="track-4")))["ok"]
    reorder.join(10)
    _settled()
    assert fake.state == int(PlaybackState.PLAYING)
    assert fake.playlist[fake.track - 1] == "file:///E:/Music/A/04.flac", (
        fake.playlist, fake.track, _start_sent(fake, n))


def test_a_pause_held_by_a_new_order_hqplayer_refuses_part_way_still_pauses(
        fake, output, monkeypatch):
    """Track 1 plays; the person swaps tracks 2 and 3 (HQPlayer plays on
    through it) and presses Pause meanwhile. HQPlayer refuses a step of the
    new order — the order is not changed — and the Pause, a word about the
    track that plays, still goes: the music stops."""
    from fastapi import HTTPException
    mgr, b = output
    player_router = _routes_on(monkeypatch, mgr)
    fake.service["PlaylistRemove"] = 0.4
    fake.refuse.add("A/02.flac")                    # the re-append of track 2
    n = len(fake.commands)
    failed = []

    def reorder():
        try:
            player_router.reorder(player_router.ReorderRequest(
                order=["track-1", "track-3", "track-2"]))
        except HTTPException as e:
            failed.append(e)
    t = threading.Thread(target=reorder, daemon=True)
    t.start()
    assert _wait(lambda: any(c == "PlaylistRemove" for c, _ in fake.commands[n:]), timeout=5)
    assert b._inflight is not None
    assert _quick(b.pause)
    t.join(10)
    assert failed and failed[0].status_code == 503
    _settled()
    assert fake.state == int(PlaybackState.PAUSED), fake.state


def test_a_tap_held_by_a_replace_that_does_not_commit_plays_on_the_queue_it_left(
        fake, output, monkeypatch):
    """A replace whose session rotation fails commits nothing; a tap made
    while it handed HQPlayer the new queue names a track the old queue holds
    too: it plays there — HQPlayer, which took the queue that never was, is
    given the queue back by the press."""
    mgr, b = output
    player_router = _routes_on(monkeypatch, mgr)
    n = _slow_adds(fake)
    failed = []

    def gone():
        raise RuntimeError("the database is away")

    def replace():
        try:
            mgr.replace_queue(_new_with_3_at_2(), play=True, archive=gone)
        except RuntimeError as e:
            failed.append(e)
    loading = threading.Thread(target=replace, daemon=True)
    loading.start()
    _handing_over(fake, n)
    assert _quick(lambda: player_router.jump(
        player_router.JumpRequest(index=3, track_id="track-3")))["ok"]
    loading.join(10)
    assert failed and not getattr(failed[0], "__notes__", [])
    _settled()
    assert [it.track_id for it in mgr.queue.snapshot()] == ["track-1", "track-2", "track-3"]
    assert fake.playlist == [f"file:///E:/Music/A/0{i}.flac" for i in (1, 2, 3)]
    assert fake.state == int(PlaybackState.PLAYING) and fake.track == 3, _start_sent(fake, n)


def test_a_pause_held_by_a_new_queue_the_lock_refuses_still_pauses(fake, output):
    """A filter change holds the command lock while HQPlayer builds it,
    playing on; a queue is put together meanwhile (the assistant's) — it
    waits its turn — and the person presses Pause. The lock refuses the new
    queue (HQPlayer builds): its Stop never went, so the Pause, held
    meanwhile, goes once the build is done — the music stops instead of
    playing on."""
    mgr, b = output
    fake.build("SetFilter", 1.5, answer="last")
    changing = threading.Thread(target=lambda: hb.change_dsp({"filter": 57, "filter1x": 57}),
                                daemon=True)
    changing.start()
    assert _wait(lambda: any(c == "SetFilter" for _, c in fake.arrivals), timeout=3)
    failed = []

    def replace():
        try:
            mgr.replace_queue(_b_album(), play=False)
        except Exception as e:
            failed.append(e)
    loading = threading.Thread(target=replace, daemon=True)
    loading.start()
    assert _wait(lambda: b._inflight is not None, timeout=3)
    assert _quick(b.pause)
    loading.join(10)
    changing.join(10)
    assert failed and isinstance(failed[0], hl.Building), (failed, fake.commands)
    assert [it.track_id for it in mgr.queue.snapshot()] == ["track-1", "track-2", "track-3"]
    assert _wait(lambda: fake.state == int(PlaybackState.PAUSED), timeout=8), fake.state


def test_a_press_asked_as_a_new_queue_begins_holding_words_is_one_of_them(fake, output,
                                                                         monkeypatch):
    """The playlist is no mirror, and the person taps the third row — a play
    press — just as a new queue begins holding words (the tap's thread
    switched out before the press registered): the press is one of the new
    queue's words, played where that queue holds the track — never a press
    the new queue then overtakes, nothing playing."""
    mgr, b = output
    player_router = _routes_on(monkeypatch, mgr)
    b.lose_mirror("a filler's wait gave up", by="refusal")
    entered, go = threading.Event(), threading.Event()
    press = b._press
    tapper, said = {}, {}

    def switched_out(target, position, **kw):
        if threading.current_thread() is tapper.get("t") and not entered.is_set():
            entered.set()
            go.wait(10)
        return press(target, position, **kw)
    monkeypatch.setattr(b, "_press", switched_out)
    t = threading.Thread(target=lambda: said.update(player_router.jump(
        player_router.JumpRequest(index=3, track_id="track-3"))), daemon=True)
    tapper["t"] = t
    t.start()
    assert entered.wait(10)
    n = _slow_adds(fake)
    loading = _loading(mgr, _new_with_3_at_2(), play=True)
    assert _wait(lambda: b._inflight is not None, timeout=5)
    go.set()
    t.join(10)
    loading.join(10)
    _settled()
    assert said.get("ok"), said
    assert fake.state == int(PlaybackState.PLAYING)
    assert fake.playlist[fake.track - 1] == "file:///E:/Music/A/03.flac", (
        fake.playlist, fake.track, _start_sent(fake, n))


# -- A tap names its track (every output) -----------------------------------------
#
# The queue sheet sends the track a row showed with its slot: the jump route
# plays that track wherever the queue holds it now, and refuses one it no
# longer holds; HQPlayer holds a tap made while a "play next" or a removal
# moves its entries.


def test_a_tap_from_a_sheet_a_new_queue_overtook_plays_the_track_it_showed(fake, output,
                                                                          monkeypatch):
    """The sheet showed [1, 2, 3]; a new queue [2, 3, 4'] is played from
    another device, and the person taps the third row before the sheet
    refreshed: the tap names track 3, which the new queue holds second — it
    plays there, not the new queue's third track."""
    mgr, b = output
    player_router = _routes_on(monkeypatch, mgr)
    mgr.replace_queue(_new_with_3_at_2(), play=True)
    assert _wait(lambda: fake.state == int(PlaybackState.PLAYING) and fake.track == 1)
    said = player_router.jump(player_router.JumpRequest(index=3, track_id="track-3"))
    assert said["ok"] and said["index"] == 2, said
    _settled()
    assert fake.playlist[fake.track - 1] == "file:///E:/Music/A/03.flac", (said, fake.track)


def test_a_tap_on_a_track_the_queue_no_longer_holds_is_refused_in_words(fake, output,
                                                                       monkeypatch):
    """The sheet showed track 1 at its first row; a new queue without it was
    played meanwhile: the tap is refused in words, nothing started."""
    mgr, b = output
    player_router = _routes_on(monkeypatch, mgr)
    mgr.replace_queue(_new_with_3_at_2(), play=True)
    assert _wait(lambda: fake.state == int(PlaybackState.PLAYING) and fake.track == 1)
    n = len(fake.commands)
    said = player_router.jump(player_router.JumpRequest(index=1, track_id="track-1"))
    assert said == {"ok": False, "index": 1, "refusal": "it is no longer in the queue"}, said
    assert _start_sent(fake, n) == []


def test_a_tap_names_its_track_on_any_output(monkeypatch):
    """Whatever the output, the slot the jump route hands it is the one that
    holds the track the sheet showed — the queue moved under the sheet."""
    from routers import player as player_router
    mgr = PlaybackManager()
    mgr.queue.replace([_item(f"E:/Music/A/0{i}.flac", i) for i in (2, 3, 1)])
    picked = []

    class Output:
        def select(self, index, track_id=None):
            picked.append(index)
            return True

        def pressing(self):
            return None
    monkeypatch.setattr(player_router, "manager", mgr)
    monkeypatch.setattr(mgr, "ensure_active", lambda: Output())
    assert player_router.jump(player_router.JumpRequest(index=1, track_id="track-1"))["ok"]
    assert picked == [3]


def test_a_tap_during_a_play_next_plays_the_tapped_track(fake, output, monkeypatch):
    """Track 1 plays; a track is queued to play next — HQPlayer takes it
    behind the playing entry, the entries after it moving down — and the
    person taps the third row meanwhile: track 3 plays, at its slot once the
    insert stands, never HQPlayer's entry 3."""
    mgr, b = output
    player_router = _routes_on(monkeypatch, mgr)
    fake.service["PlaylistRemove"] = 0.4
    n = len(fake.commands)
    inserting = threading.Thread(target=lambda: mgr.append(
        [_item("E:/Music/N/01.flac", 31)], "next"), daemon=True)
    inserting.start()
    assert _wait(lambda: any(c == "PlaylistRemove" for c, _ in fake.commands[n:]), timeout=5)
    said = _quick(lambda: player_router.jump(
        player_router.JumpRequest(index=3, track_id="track-3")))
    assert said["ok"], said
    inserting.join(10)
    _settled()
    assert [it.track_id for it in mgr.queue.snapshot()] == [
        "track-1", "track-31", "track-2", "track-3"]
    assert fake.playlist[fake.track - 1] == "file:///E:/Music/A/03.flac", (
        fake.playlist, fake.track, _start_sent(fake, n))


def test_a_tap_during_a_removal_plays_the_tapped_track(fake, output, monkeypatch):
    """Track 1 plays; the person removes track 2 — HQPlayer takes the
    removal, the entries after it moving up — and taps the third row
    meanwhile: track 3 plays, at its slot once the removal stands."""
    mgr, b = output
    player_router = _routes_on(monkeypatch, mgr)
    fake.service["PlaylistRemove"] = 0.6
    n, came = len(fake.commands), len(fake.arrivals)
    removing = threading.Thread(target=lambda: mgr.remove(2, "track-2"), daemon=True)
    removing.start()
    assert _wait(lambda: "PlaylistRemove" in [c for _, c in fake.arrivals[came:]], timeout=5)
    said = _quick(lambda: player_router.jump(
        player_router.JumpRequest(index=3, track_id="track-3")))
    assert said["ok"], said
    removing.join(10)
    _settled()
    assert [it.track_id for it in mgr.queue.snapshot()] == ["track-1", "track-3"]
    assert fake.playlist[fake.track - 1] == "file:///E:/Music/A/03.flac", (
        fake.playlist, fake.track, _start_sent(fake, n))


def test_a_tap_handed_on_while_a_removal_runs_finds_its_track_once_it_stands(fake, output,
                                                                            monkeypatch):
    """The route hands a tap on as the sheet sent it while a removal is in
    flight — the output finds the track among what it is handed — and the
    removal stands before HQPlayer reads the tap: HQPlayer finds track 3
    where the queue holds it now, never refuses the third row as gone."""
    mgr, b = output
    player_router = _routes_on(monkeypatch, mgr)
    fake.service["PlaylistRemove"] = 0.6
    n, came = len(fake.commands), len(fake.arrivals)
    select, handed = b.select, []

    def once_the_removal_stands(index, track_id=None):
        handed.append(index)
        removing.join(10)
        return select(index, track_id)
    monkeypatch.setattr(b, "select", once_the_removal_stands)
    removing = threading.Thread(target=lambda: mgr.remove(2, "track-2"), daemon=True)
    removing.start()
    assert _wait(lambda: "PlaylistRemove" in [c for _, c in fake.arrivals[came:]], timeout=5)
    said = player_router.jump(player_router.JumpRequest(index=3, track_id="track-3"))
    assert said["ok"], said
    assert handed == [3]                        # the route left the track to the output
    _settled()
    assert [it.track_id for it in mgr.queue.snapshot()] == ["track-1", "track-3"]
    assert fake.playlist[fake.track - 1] == "file:///E:/Music/A/03.flac", (
        fake.playlist, fake.track, _start_sent(fake, n))


def test_a_tap_on_a_track_queued_twice_means_the_slot_nearest_the_row():
    """A track queued twice: the row tapped holds it — that slot; moved
    under the sheet, the slot nearest the row that holds it."""
    q = CanonicalQueue()
    q.replace([_item(f"E:/Music/A/0{i}.flac", i) for i in (1, 2, 3)]
              + [_item("E:/Music/A/01.flac", 1)])
    assert q.slot_of(4, "track-1") == 4
    assert q.slot_of(3, "track-1") == 4
    assert q.slot_of(2, "track-1") == 1
    assert q.slot_of(2, "track-9") is None
    assert q.slot_of(9, None) is None


# -- The mirror's readings: a reading HQPlayer gave, a judge that ran ------------


def test_a_play_next_hqplayer_reads_nothing_for_loses_the_mirror(fake, output, monkeypatch):
    """HQPlayer answers nothing to the PlaylistGet a "play next" begins with
    (busy): no reading is no shorter playlist — the insert is the queue's,
    the mirror lost, and HQPlayer gets the queue back at its next answer;
    never drift, which nothing mends."""
    mgr, b = output
    losses = b.mirror_losses
    _link().client.timeout = 0.5                      # a command's wait, shortened
    fake.service["PlaylistGet"] = 1.0                 # busy: the read goes unanswered
    try:
        assert mgr.append([_item("E:/Music/N/01.flac", 31)], "next") == 1
    finally:
        del fake.service["PlaylistGet"]
        _link().client.timeout = hl.COMMAND_WAIT_S
    assert [it.track_id for it in mgr.queue.snapshot()] == [
        "track-1", "track-31", "track-2", "track-3"]
    assert b._mirror == "lost" and b.mirror_losses == losses + 1, b._mirror
    assert _wait(lambda: fake.playlist == [
        "file:///E:/Music/A/01.flac", "file:///E:/Music/N/01.flac",
        "file:///E:/Music/A/02.flac", "file:///E:/Music/A/03.flac"], timeout=8), fake.playlist


@pytest.mark.parametrize("moved", ["a slot's way in read again", "a stream landed"])
def test_a_new_order_is_taken_over_a_queue_whose_slots_did_not_move(fake, output,
                                                                    monkeypatch, moved):
    """Between the route's read and the apply the queue changed in a way that
    moves no slot — each slot's way in read again (a press's first step, a
    reconverge), or a stream landed for one: the new order is still the
    queue's to take, never refused as made on another queue."""
    mgr, b = output
    player_router = _routes_on(monkeypatch, mgr)
    apply = mgr.apply_reorder

    def changed_first(plan):
        if moved == "a slot's way in read again":
            with mgr._mutate_lock:
                b._ways_in()
        else:
            assert mgr.queue.set_play(mgr.queue.item_at(3), None) == 3
        return apply(plan)
    monkeypatch.setattr(mgr, "apply_reorder", changed_first)
    said = player_router.reorder(player_router.ReorderRequest(
        order=["track-1", "track-3", "track-2"]))
    assert said["ok"]
    assert [it.track_id for it in mgr.queue.snapshot()] == ["track-1", "track-3", "track-2"]


def test_an_entry_not_ours_is_judged_again_when_its_first_judge_read_nothing(
        fake, output, monkeypatch):
    """Another controller's entry is read while in step; the at-once judge's
    playlist read fails that tick (HQPlayer answers PlaylistGet with an
    error): the canary never judged, so the next answer asks again — the
    playlist is drifted before a tap selects into it."""
    mgr, b = output
    monkeypatch.setattr(hb, "DRIFT_CHECK_EVERY", 10 ** 6)        # no canary of its cadence
    fake.replies["PlaylistGet"] = '<PlaylistGet result="Error">busy</PlaylistGet>'
    n = len(fake.commands)
    with fake._lock:
        fake.playlist.append("file:///Elsewhere/theirs.flac")
        fake.track = len(fake.playlist)
    assert _wait(lambda: any(c == "PlaylistGet" for c, _ in fake.commands[n:]), timeout=5)
    del fake.replies["PlaylistGet"]
    assert _wait(lambda: b._mirror == "drift", timeout=5), b._mirror


def test_a_new_order_at_a_spot_on_a_gap_leaves_the_spot_on_its_item(
        fake, fake_b, two_libraries, monkeypatch):
    """Stopped at a spot on a gap — the slot the queue stood on, held only by
    the other HQPlayer — the person makes a new order: the spot stays on its
    item, the status the same track, Next counting from it."""
    mgr, settings_router, queue = two_libraries
    player_router = _routes_on(monkeypatch, mgr)
    mgr.queue.replace(queue + [_item("E:/Music/A/05.flac", 5)])
    mgr._last_slot = 3
    _pick(settings_router, type="hqplayer", hqplayer={"host": "127.0.0.1", "port": fake_b.port})
    b = mgr.active
    assert _wait(lambda: mgr._latest_status.get("track_index") == 3)
    gap = mgr.queue.item_at(3)
    assert b._spot is not None and b._spot[0] is gap
    order = ["track-1", "track-3", "track-2", "track-5", "track-4"]
    player_router.reorder(player_router.ReorderRequest(order=order))
    assert [it.track_id for it in mgr.queue.snapshot()] == order
    assert b._spot is not None and b._spot[0] is gap, (b._spot and b._spot[0].track_id)


def test_a_new_order_behind_a_tail_entry_queued_twice_is_the_queues(fake, output,
                                                                    monkeypatch):
    """A tail mirror whose entry read is a file queued at two slots: which
    one HQPlayer reads is no verdict, so a new order behind it lands on no
    copy by a guess — like a removal there, it is the queue's, the mirror
    lost — never refused as an order HQPlayer did not take."""
    mgr, b = output
    player_router = _routes_on(monkeypatch, mgr)
    _tail(mgr, b, fake)
    assert mgr.append([_item("E:/Music/A/02.flac", 2)]) == 1          # track-2 queued again
    assert _wait(lambda: mgr._latest_status.get("track_index") == 2)
    held = list(fake.playlist)
    order = ["track-1", "track-2", "track-4", "track-3", "track-2"]
    said = player_router.reorder(player_router.ReorderRequest(order=order))
    assert said["ok"]
    assert [it.track_id for it in mgr.queue.snapshot()] == order
    assert fake.playlist == held and b._mirror == "lost"


def test_a_new_order_behind_a_tail_entry_hqplayer_moved_on_from_is_the_queues(fake, output,
                                                                              monkeypatch):
    """A tail mirror: a new order is planned behind the slot playing, and
    HQPlayer moves on to its next entry by itself before the edit reads it.
    The entry it reads is not the plan's, so the order lands nowhere by a
    guess: it is the queue's, the mirror lost — never refused as an order
    HQPlayer did not take."""
    mgr, b = output
    player_router = _routes_on(monkeypatch, mgr)
    _tail(mgr, b, fake)
    assert _wait(lambda: mgr._latest_status.get("track_index") == 2)
    held = list(fake.playlist)
    apply = mgr.apply_reorder

    def moved_on_first(plan):
        with fake._lock:                        # HQPlayer's own advance, not read yet
            fake.track, fake.position = 4, 0.0
        return apply(plan)
    monkeypatch.setattr(mgr, "apply_reorder", moved_on_first)
    order = ["track-1", "track-2", "track-4", "track-3"]
    said = player_router.reorder(player_router.ReorderRequest(order=order))
    assert said["ok"]
    assert [it.track_id for it in mgr.queue.snapshot()] == order
    assert fake.playlist == held and b._mirror == "lost"


def test_a_new_order_hqplayer_is_too_busy_for_is_news_not_a_fault(fake, output, monkeypatch,
                                                                   caplog):
    """A new order refused while HQPlayer builds answers 409 — the output's
    own refusal, logged as news, never as a fault with its traceback."""
    from fastapi import HTTPException
    mgr, b = output
    player_router = _routes_on(monkeypatch, mgr)
    fake.build("SetFilter", 2.0, answer="last")
    assert hb.change_dsp({"filter": 57, "filter1x": 57})["building"] is not None
    with pytest.raises(HTTPException) as refused:
        player_router.reorder(player_router.ReorderRequest(
            order=["track-2", "track-1", "track-3"]))
    assert refused.value.status_code == 409
    assert not [r for r in caplog.records if r.name == "routers.player"
                and r.levelno >= logging.ERROR], caplog.text


def test_a_close_as_the_connection_passes_on_closes_no_socket_another_request_holds(
        fake, monkeypatch):
    """A request lets the connection go to the next one waiting for it, and
    the link is closed as that next request reads its answer. The socket is
    the next request's now: the first one, done with it, closes nothing
    under the reader — the read gets its answer, and no loss is counted."""
    link = _link()
    c = link.client
    with link.asking() as hqp:                   # the connection made
        assert hqp.get_status() is not None
    threads = {}
    a_holds, a_go = threading.Event(), threading.Event()
    gave, b_ready, b_go = threading.Event(), threading.Event(), threading.Event()
    read, wait_on = c._read_response, c._wait

    def holding(wait=None, on_late=None):
        out = read(wait, on_late)
        if threading.current_thread() is threads.get("a"):
            a_holds.set()                        # answered; the connection still its
            a_go.wait(5)
        return out

    def answer_there(sock, event, timeout):
        got = wait_on(sock, event, timeout)
        if (threading.current_thread() is threads.get("b") and got == "ready"
                and not b_ready.is_set()):
            b_ready.set()                        # B's answer is there, not read yet
            b_go.wait(5)
        return got
    monkeypatch.setattr(c, "_read_response", holding)
    monkeypatch.setattr(c, "_wait", answer_there)

    class Paused(hl.HqpLink):
        # A's look at the link's state once it let the connection go
        @property
        def state(self):
            if (threading.current_thread() is threads.get("a")
                    and sys._getframe(1).f_code.co_name == "_give"
                    and not self._conn._is_owned()):
                gave.wait(5)
            return self.__dict__["state"]

        @state.setter
        def state(self, value):
            self.__dict__["state"] = value
    link.__class__ = Paused
    out = {}

    def ask(name):
        with link.asking() as hqp:
            out[name] = hqp.get_status()
    threads["a"] = threading.Thread(target=ask, args=("a",), daemon=True)
    threads["b"] = threading.Thread(target=ask, args=("b",), daemon=True)
    threads["a"].start()
    assert a_holds.wait(5)
    threads["b"].start()
    assert _wait(lambda: bool(link._waiting), timeout=5)       # B waits its turn
    a_go.set()                                   # A lets the connection go
    assert b_ready.wait(5)                       # B holds it; its answer is there
    link.close("the node is stopping")
    gave.set()
    threads["a"].join(5)
    b_go.set()
    threads["b"].join(5)
    assert out.get("b") is not None and c.losses == 0, (out, c.losses)


# -- Held words go as they were held ---------------------------------------------------
#
# The words a person made while a queue mutation handed HQPlayer its playlist go
# under the number they were held with — never numbered again, never dropped by a
# word of their own — and a Next with nothing to start moves a spot, as it would
# on the queue as it stands.


def _said_by_queue_start(monkeypatch, b) -> list:
    """What each carry-out could not do, as queue_start answers it."""
    said, start = [], b.queue_start

    def recorded():
        out = start()
        said.append(out)
        return out
    monkeypatch.setattr(b, "queue_start", recorded)
    return said


@pytest.mark.parametrize("edit", ["radio", "new order"])
def test_a_pause_then_a_seek_held_by_a_mutation_that_plays_on_both_go(fake, output,
                                                                       monkeypatch, edit):
    """HQPlayer plays track 1 on through a mutation that never stops it — the
    radio's clear, or a seamless new order behind the playing track — and the
    person presses Pause, then drags the position to 60 s. Once the queue
    stands both go: HQPlayer paused, at 60 s."""
    mgr, b = output
    player_router = _routes_on(monkeypatch, mgr)
    n, came = len(fake.commands), len(fake.arrivals)
    if edit == "radio":
        fake.service["PlaylistClear"] = 0.6
        t = threading.Thread(target=lambda: mgr.clear_for_radio(lambda: None), daemon=True)
        t.start()
        assert _wait(lambda: "PlaylistClear" in [c for _, c in fake.arrivals[came:]], timeout=5)
    else:
        fake.service["PlaylistRemove"] = 0.4
        t = threading.Thread(target=lambda: player_router.reorder(
            player_router.ReorderRequest(order=["track-1", "track-3", "track-2"])), daemon=True)
        t.start()
        assert _wait(lambda: any(c == "PlaylistRemove" for c, _ in fake.commands[n:]), timeout=5)
    assert b._inflight is not None
    assert _quick(b.pause)
    assert _quick(lambda: b.seek(60))
    t.join(10)
    _settled()
    sent = [(c, a) for c, a in fake.commands[n:] if c in ("Pause", "Seek", "Play", "Stop")]
    assert fake.state == int(PlaybackState.PAUSED), (fake.state, sent)
    assert ("Seek", {"position": "60"}) in sent, sent


def test_a_held_pause_never_undoes_a_newer_play_waiting_its_turn(fake, output, monkeypatch):
    """A Pause held while HQPlayer takes a new order around the playing
    track; the order stands. Right then a slow volume change holds the
    command lock, and the person presses Play — asked after the Pause, it
    waits its turn. The held Pause is the older word: HQPlayer plays on."""
    mgr, b = output
    player_router = _routes_on(monkeypatch, mgr)
    fake.service["PlaylistRemove"] = 0.4
    start, link, later = b.queue_start, _link(), {}

    def a_play_waits_its_turn():
        if threading.current_thread() is reordering:
            fake.service["Volume"] = 1.5
            threading.Thread(target=lambda: b.set_volume(-10), daemon=True).start()
            assert _wait(lambda: link.lock.owner is not None, timeout=3)      # the volume holds it
            p = threading.Thread(target=lambda: later.update(ok=b.play()), daemon=True)
            p.start()
            assert _wait(lambda: len(link.lock._waiting) == 1, timeout=3)     # the Play's turn
            later["t"] = p
        return start()
    monkeypatch.setattr(b, "queue_start", a_play_waits_its_turn)
    n = len(fake.commands)
    reordering = threading.Thread(target=lambda: player_router.reorder(
        player_router.ReorderRequest(order=["track-1", "track-3", "track-2"])), daemon=True)
    reordering.start()
    assert _wait(lambda: any(c == "PlaylistRemove" for c, _ in fake.commands[n:]), timeout=5)
    assert _quick(b.pause)
    reordering.join(15)
    later["t"].join(10)
    _settled()
    sent = [c for c, _ in fake.commands[n:] if c in ("Pause", "Play", "Volume")]
    assert fake.state == int(PlaybackState.PLAYING), (fake.state, sent)


def test_a_held_seek_that_meets_a_build_after_its_pause_went_goes_after_it(fake, output,
                                                                           monkeypatch):
    """A Pause and a Seek held while HQPlayer takes a new order around the
    playing track. Carried out once the order stands: the Pause goes, and a
    DSP change made right then sets off a build the Seek meets — it goes
    once the build is done, its own Pause no newer word that drops it."""
    mgr, b = output
    player_router = _routes_on(monkeypatch, mgr)
    fake.service["PlaylistRemove"] = 0.4
    n, came = len(fake.commands), len(fake.arrivals)
    reordering = threading.Thread(target=lambda: player_router.reorder(
        player_router.ReorderRequest(order=["track-1", "track-3", "track-2"])), daemon=True)
    reordering.start()
    assert _wait(lambda: any(c == "PlaylistRemove" for c, _ in fake.commands[n:]), timeout=5)
    assert _quick(b.pause)
    assert _quick(lambda: b.seek(30))
    fake.service["Pause"] = 0.5                 # the held Pause, carried out, answered slowly
    assert _wait(lambda: "Pause" in [c for _, c in fake.arrivals[came:]], timeout=10)
    fake.build("SetFilter", 2.0)                # a DSP change made right then builds
    answer = hb.change_dsp({"filter": 57, "filter1x": 57})
    reordering.join(10)
    assert answer.get("building"), answer
    assert _wait(lambda: ("Seek", {"position": "30"}) in fake.commands[n:], timeout=8), (
        [c for c, _ in fake.commands[n:] if c not in _QUESTIONS])


def test_a_pause_the_lock_refuses_during_a_build_leaves_the_changes_resume(fake, output):
    """A filter change stops HQPlayer for its build and owes the resume of
    the track that played. The person presses Pause meanwhile: refused at
    once (HQPlayer builds) — a command the lock refuses supersedes nothing,
    and the change's resume goes once the build is done."""
    mgr, b = output
    fake.build("SetFilter", 2.0, answer="last", stops=True)
    assert hb.change_dsp({"filter": 57, "filter1x": 57})["building"] is not None
    n = len(fake.commands)
    with pytest.raises(hl.Building):
        b.pause()
    assert _wait(lambda: ("SelectTrack", {"index": "1"}) in _start_sent(fake, n), timeout=8), \
        "the change's resume never went"
    assert _wait(lambda: fake.state == int(PlaybackState.PLAYING) and fake.track == 1)


def test_a_pause_held_by_the_radios_clear_outlives_a_dsp_changes_resume(fake, output):
    """The person pauses while the radio's clear is on its way; a filter
    change made after it builds, and HQPlayer stops for the build. The change
    found HQPlayer playing only because the Pause was held: it resumes
    nothing, and the music stays stopped."""
    mgr, b = output
    fake.service["PlaylistClear"] = 0.6
    fake.build("SetFilter", 2.0, stops=True)
    radio = threading.Thread(target=lambda: mgr.clear_for_radio(lambda: None), daemon=True)
    came = len(fake.arrivals)
    radio.start()
    assert _wait(lambda: b._inflight is not None
                 and "PlaylistClear" in [c for _, c in fake.arrivals[came:]], timeout=5)
    n = len(fake.commands)
    assert b.pause()
    changing = threading.Thread(target=lambda: hb.change_dsp({"filter": 57}), daemon=True)
    changing.start()
    radio.join(10)
    changing.join(10)
    assert _wait(lambda: "SetFilter" in [c for c, _ in fake.commands[n:]], timeout=5)
    assert _wait(lambda: fake._silent_until <= time.monotonic(), timeout=8)    # the build is over
    _settled()
    sent = [c for c, _ in fake.commands[n:] if c not in _QUESTIONS]
    assert fake.state != int(PlaybackState.PLAYING), (fake.state, sent)
    assert not [c for c in sent if c in ("SelectTrack", "Play")], sent


def test_a_next_during_a_play_next_at_a_spot_only_moves_the_spot(fake, listens, monkeypatch):
    """HQPlayer stopped at a spot on track 2; a track is queued to play next
    behind it, and the person presses Next while HQPlayer takes the insert:
    as at a spot with nothing in flight, the spot moves to the next slot —
    the new track — and nothing sounds."""
    mgr = _album(3)
    b = _attach(mgr)
    try:
        player_router = _stopped_at_spot(fake, mgr, monkeypatch)
        fake.service["PlaylistRemove"] = 0.6
        n, came = len(fake.commands), len(fake.arrivals)
        inserting = threading.Thread(target=lambda: mgr.append(
            [_item("E:/Music/N/01.flac", 31)], "next"), daemon=True)
        inserting.start()
        assert _wait(lambda: "PlaylistRemove" in [c for _, c in fake.arrivals[came:]], timeout=5)
        assert b._inflight is not None
        assert _quick(player_router.next_track)["ok"]
        inserting.join(10)
        _settled()
        assert [it.track_id for it in mgr.queue.snapshot()] == [
            "track-3", "track-2", "track-31", "track-1"]
        assert _start_sent(fake, n) == [] and fake.state == int(PlaybackState.STOPPED)
        assert _wait(lambda: mgr._latest_status.get("resume") == {"index": 3, "position": 0})
    finally:
        b.shutdown()


def test_a_next_during_a_paused_reorders_rebuild_only_moves_the_spot(fake, listens,
                                                                     monkeypatch):
    """Paused on slot 2 at 2:30; a new order needs a rebuild, which leaves
    HQPlayer stopped at the spot. Next pressed while HQPlayer takes it: the
    spot moves to the next slot, nothing sounds."""
    mgr = _album(3)
    player_router = _routes_on(monkeypatch, mgr)
    b = _attach(mgr)
    try:
        _paused_on_2_at_150(fake, mgr)
        n = _slow_adds(fake)
        reorder = threading.Thread(target=lambda: player_router.reorder(
            player_router.ReorderRequest(order=["track-3", "track-2", "track-1"])), daemon=True)
        reorder.start()
        _handing_over(fake, n)
        assert b._inflight is not None
        assert _quick(player_router.next_track)["ok"]
        reorder.join(10)
        _settled()
        assert _start_sent(fake, n) == [] and fake.state == int(PlaybackState.STOPPED)
        assert _wait(lambda: mgr._latest_status.get("resume") == {"index": 3, "position": 0})
    finally:
        b.shutdown()


def test_a_previous_while_a_restarted_hqplayer_gets_the_queue_back_moves_nothing(fake, output,
                                                                                monkeypatch):
    """HQPlayer restarted under the playing track — stopped, its playlist
    empty, no spot of ours — and the reconverge hands it the queue afresh;
    the person presses Previous meanwhile. Nothing to count from: the word
    moves nothing and starts nothing, and HQPlayer gets the queue back whole
    — never a fault answered 503."""
    mgr, b = output
    player_router = _routes_on(monkeypatch, mgr)
    n = _slow_adds(fake)
    fake.restart()
    assert _wait(lambda: (b.poke(), b._inflight is not None)[1], timeout=10)
    assert _quick(player_router.previous_track)["ok"]
    _settled()
    assert _wait(lambda: fake.playlist == [f"file:///E:/Music/A/0{i}.flac" for i in (1, 2, 3)])
    assert _start_sent(fake, n) == [] and fake.state == int(PlaybackState.STOPPED)


def test_a_tap_held_by_a_new_queue_hqplayer_refused_whole_is_said_with_its_refusal(
        fake, output, monkeypatch):
    """HQPlayer refuses every file of a new queue — nothing commits — and a
    tap made while it was handed the album names a track only that album
    holds: the replace is answered in HQPlayer's words, the tap's "not
    played" with them."""
    from playback.manager import QueueRefused
    mgr, b = output
    player_router = _routes_on(monkeypatch, mgr)
    fake.refuse.add("B/0")
    n = _slow_adds(fake)
    out = {}

    def replace():
        try:
            out["result"] = mgr.replace_queue(_b_album(), play=True)
        except Exception as e:
            out["error"] = e
    loading = threading.Thread(target=replace, daemon=True)
    loading.start()
    _handing_over(fake, n)
    assert _quick(lambda: player_router.jump(
        player_router.JumpRequest(index=2, track_id="track-12")))["ok"]
    loading.join(10)
    _settled()
    assert [it.track_id for it in mgr.queue.snapshot()] == ["track-1", "track-2", "track-3"]
    error = out.get("error")
    assert isinstance(error, QueueRefused) and "refused all 3 tracks" in str(error), out
    assert error.__notes__ == ["Song 12, played meanwhile, is not in the queue — not played"]


def test_a_next_held_by_a_play_next_hqplayer_refused_whole_steps_on_the_queue_as_it_stands(
        fake, output, monkeypatch):
    """Track 1 plays; a track queued to play next is refused by HQPlayer —
    the queue takes nothing. Next pressed while HQPlayer took the insert
    steps on the queue as it stands: track 2 plays."""
    mgr, b = output
    player_router = _routes_on(monkeypatch, mgr)
    fake.service["PlaylistRemove"] = 0.4
    fake.refuse.add("N/01.flac")
    came, out = len(fake.arrivals), {}
    inserting = threading.Thread(target=lambda: out.update(added=mgr.append(
        [_item("E:/Music/N/01.flac", 31)], "next")), daemon=True)
    inserting.start()
    assert _wait(lambda: "PlaylistRemove" in [c for _, c in fake.arrivals[came:]], timeout=5)
    assert b._inflight is not None
    assert _quick(player_router.next_track)["ok"]
    inserting.join(10)
    _settled()
    assert out.get("added") == 0, out
    assert [it.track_id for it in mgr.queue.snapshot()] == ["track-1", "track-2", "track-3"]
    assert _wait(lambda: fake.state == int(PlaybackState.PLAYING)
                 and fake.playlist[fake.track - 1] == "file:///E:/Music/A/02.flac"), (
        fake.state, fake.track, fake.playlist)


def test_seek_pause_play_during_the_radios_clear_keeps_the_seek(fake, output):
    """Seek to 60 s, Pause, Play while the radio's clear is on its way, the
    seed playing on: the Seek moved what plays, the Pause held it there, the
    Play resumed it — HQPlayer plays at 60 s."""
    mgr, b = output
    fake.service["PlaylistClear"] = 0.6
    n, came = len(fake.commands), len(fake.arrivals)
    radio = threading.Thread(target=lambda: mgr.clear_for_radio(lambda: None), daemon=True)
    radio.start()
    assert _wait(lambda: "PlaylistClear" in [c for _, c in fake.arrivals[came:]], timeout=5)
    assert b._inflight is not None
    assert _quick(lambda: b.seek(60))
    assert _quick(b.pause)
    assert _quick(b.play)
    radio.join(10)
    _settled()
    sent = [(c, a) for c, a in fake.commands[n:] if c not in _QUESTIONS]
    assert fake.state == int(PlaybackState.PLAYING)
    assert ("Seek", {"position": "60"}) in sent, sent


def test_held_words_a_newer_word_overtook_do_not_go(fake, output):
    """Held words carried out once a newer word of the person's holds: none
    goes — not even a Play that would start a play press on a playlist that
    is no mirror (lost to a refusal, HQPlayer stopped), which no request
    gate sees before it."""
    mgr, b = output
    b.stop()
    _stopped(mgr, fake)
    b.lose_mirror("HQPlayer refused all 1 tracks", by="refusal")
    held = _link().accept("transport")          # the word's number, as it was held
    assert b.pause()                            # a newer word, sent at once
    n = len(fake.commands)
    b._send_words([(held, "Play", None)])
    _settled()
    assert b._pressed is None
    assert not [c for c, _ in fake.commands[n:] if c not in _QUESTIONS], fake.commands[n:]


# The URIs failing ends the reconverge's thread with that error: the scenario's premise
@pytest.mark.filterwarnings("ignore::pytest.PytestUnhandledThreadExceptionWarning")
def test_a_rebuild_whose_uris_fail_leaves_no_number_pending(fake, output, monkeypatch):
    """HQPlayer back stopped with an empty playlist: the reconverge numbers its
    rebuild as it begins holding words, then makes each slot's URI — and that
    fails (a media port another process holds, in stream mode). Nothing was
    taken afresh, so no number of it stays pending: a start made before it,
    still owed, is not superseded by a rebuild that never was."""
    mgr, b = output
    _stopped(mgr, fake)
    with fake._lock:
        fake.playlist, fake.track = [], 0
    link = _link()
    uri_for, tried = b._uri_for, threading.Event()

    def media_port_taken(item):
        if threading.current_thread().name == "hqp-reconverge":
            tried.set()
            raise RuntimeError("media port 8830 is already in use by another process")
        return uri_for(item)
    monkeypatch.setattr(b, "_uri_for", media_port_taken)
    older = link.item("start", link.accept("transport"))    # a start owed from before
    b.lose_mirror("HQPlayer came back with a different playlist (restarted?)")
    assert tried.wait(10), "the reconverge never made its URIs"
    assert _wait(lambda: not any(t.name == "hqp-reconverge" for t in threading.enumerate()))
    assert link.superseded(older) is None, (link.superseded(older), link._pending)
    assert link._pending == {}, link._pending


# -- A new order names slots, on the output it was made for ----------------------------


@pytest.mark.parametrize("order", [["track-2", "track-1", "track-3"],    # a rebuild
                                   ["track-1", "track-3", "track-2"]])   # seamless
def test_a_new_order_planned_on_one_hqplayer_is_refused_on_another(fake, fake_b, output,
                                                                   monkeypatch, order):
    """A plays; a new order is planned while it does, and the person picks
    HQPlayer B before the order is applied. The plan's position and resume
    were A's: on B it is refused whole (409), and B stays stopped as a
    switch leaves it."""
    from fastapi import HTTPException
    mgr, b = output                                 # A plays slot 1
    settings_router = _picker(monkeypatch, fake)
    player_router = _routes_on(monkeypatch, mgr)
    apply = mgr.apply_reorder

    def switched_first(plan):
        _pick(settings_router, type="hqplayer", hqplayer={"host": "127.0.0.1",
                                                          "port": fake_b.port})
        assert mgr.active is not b and fake_b.state == int(PlaybackState.STOPPED)
        return apply(plan)
    monkeypatch.setattr(mgr, "apply_reorder", switched_first)
    try:
        with pytest.raises(HTTPException) as refused:
            player_router.reorder(player_router.ReorderRequest(order=order))
        assert refused.value.status_code == 409, refused.value.detail
        _settled()
        assert fake_b.state == int(PlaybackState.STOPPED), (fake_b.track, fake_b.position)
        assert [it.track_id for it in mgr.queue.snapshot()] == ["track-1", "track-2", "track-3"]
    finally:
        if mgr.active is not None:
            mgr.active.shutdown()


def test_a_new_order_over_a_rebind_that_cleared_a_track_moves_that_slot(fake, output,
                                                                       monkeypatch):
    """Between the route's read and the apply, slot 3's file left the
    library with its track — re-bound in place to a pass-through entry, its
    track id cleared. The new order names slots, not track ids: that slot
    goes where the order put it, and HQPlayer and the queue agree."""
    mgr, b = output                                 # playing slot 1 of 3
    player_router = _routes_on(monkeypatch, mgr)
    apply = mgr.apply_reorder

    def rebound_first(plan):
        with mgr._mutate_lock, b.mutation():         # as rebind_files does
            moved = mgr.queue.rebind(
                lambda items: [{"track_id": None, "media_file_id": None,
                                "source": {"kind": "uri", "uri": "file:///E:/Music/A/03.flac"}}
                               if i == 3 else None for i, _ in enumerate(items, start=1)],
                None)
            assert len(moved) == 1
            b.queue_changed("rebind")
        b.queue_start()
        return apply(plan)
    monkeypatch.setattr(mgr, "apply_reorder", rebound_first)
    assert player_router.reorder(player_router.ReorderRequest(
        order=["track-1", "track-3", "track-2"]))["ok"]
    queue = [it.source.get("uri") or "file:///" + it.source["path"]
             for it in mgr.queue.snapshot()]
    assert queue == ["file:///E:/Music/A/01.flac", "file:///E:/Music/A/03.flac",
                     "file:///E:/Music/A/02.flac"], queue
    assert b._mirror == "step" and fake.playlist == queue, (fake.playlist, b._mirror)


def test_a_new_order_of_a_track_queued_from_two_rips_hands_each_rip(fake, output, monkeypatch):
    """Track 2 queued twice from two rips — another album's copy: the same
    track, another file. A new order hands HQPlayer each rip at the slot the
    order gives it, and HQPlayer and the queue agree."""
    mgr, b = output                                 # playing slot 1 of 3
    player_router = _routes_on(monkeypatch, mgr)
    other = QueueItem(track_id="track-2", media_file_id=22,
                      source={"kind": "file", "path": "E:/Music/C/02.flac", "format": "FLAC"},
                      title="Song 2", artist="Artist", album="Compilation")
    assert mgr.append([other]) == 1
    assert player_router.reorder(player_router.ReorderRequest(
        order=["track-1", "track-3", "track-2", "track-2"]))["ok"]
    queue = ["file:///" + it.source["path"] for it in mgr.queue.snapshot()]
    assert queue == ["file:///E:/Music/A/01.flac", "file:///E:/Music/A/03.flac",
                     "file:///E:/Music/A/02.flac", "file:///E:/Music/C/02.flac"], queue
    assert b._mirror == "step" and fake.playlist == queue, fake.playlist


def test_a_tap_on_the_second_copy_during_a_new_order_plays_that_copy(fake, output,
                                                                     monkeypatch):
    """Track 1 queued twice — [1, 2, 3, 1'], the first copy playing — and a
    seamless new order [1, 3, 2, 1'] goes to HQPlayer; the person taps the
    fourth row (the second copy) meanwhile: HQPlayer plays its fourth entry,
    never the playing first copy restarted."""
    mgr, b = output
    player_router = _routes_on(monkeypatch, mgr)
    assert mgr.append([_item("E:/Music/A/01.flac", 1)], "end") == 1
    assert len(fake.playlist) == 4
    fake.service["PlaylistRemove"] = 0.4
    n = len(fake.commands)
    reorder = threading.Thread(target=lambda: player_router.reorder(player_router.ReorderRequest(
        order=["track-1", "track-3", "track-2", "track-1"])), daemon=True)
    reorder.start()
    assert _wait(lambda: any(c == "PlaylistRemove" for c, _ in fake.commands[n:]), timeout=5)
    assert b._inflight is not None
    said = _quick(lambda: player_router.jump(
        player_router.JumpRequest(index=4, track_id="track-1")))
    reorder.join(10)
    _settled()
    assert [it.track_id for it in mgr.queue.snapshot()] == [
        "track-1", "track-3", "track-2", "track-1"]
    assert fake.state == int(PlaybackState.PLAYING) and fake.track == 4, (
        said, fake.track, _start_sent(fake, n))


def test_a_new_order_hqplayer_refuses_is_said_once_in_its_words(fake, output, monkeypatch,
                                                                caplog):
    """HQPlayer refuses a step of a new order: the route answers its words,
    and the refusal is logged once, as HQPlayer's answer — never again as a
    fault with a traceback."""
    from fastapi import HTTPException
    mgr, b = output
    player_router = _routes_on(monkeypatch, mgr)
    fake.replies["PlaylistRemove"] = '<PlaylistRemove result="Error">busy</PlaylistRemove>'
    with pytest.raises(HTTPException) as refused:
        player_router.reorder(player_router.ReorderRequest(
            order=["track-1", "track-3", "track-2"]))
    assert refused.value.status_code == 503 and "did not take the new order" in str(
        refused.value.detail), refused.value.detail
    said = [r for r in caplog.records if "did not take the new order: busy" in r.getMessage()]
    assert len(said) == 1 and said[0].levelno == logging.WARNING, [
        (r.name, r.levelname, r.getMessage()) for r in said]
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR], caplog.text


# -- What HQPlayer did not answer, or moved on from, in step -----------------------------


def test_a_seamless_new_order_whose_removal_hqplayer_left_unanswered_is_the_queues(
        fake, output, monkeypatch):
    """In step, track 1 plays; the person swaps tracks 2 and 3. HQPlayer
    leaves the first removal unanswered within its wait, and carries it out
    once its silence is over: the new order is the queue's, the mirror lost,
    and HQPlayer gets the queue back at its next answer — never a refusal
    that leaves the queue in its old order and HQPlayer short of a track."""
    mgr, b = output
    player_router = _routes_on(monkeypatch, mgr)
    _link().client.timeout = 0.5                      # a command's wait, shortened
    fake.service["PlaylistRemove"] = 1.0              # busy: the removal goes unanswered
    try:
        said = player_router.reorder(player_router.ReorderRequest(
            order=["track-1", "track-3", "track-2"]))
    finally:
        del fake.service["PlaylistRemove"]
        _link().client.timeout = hl.COMMAND_WAIT_S
    assert said["ok"], said
    assert [it.track_id for it in mgr.queue.snapshot()] == ["track-1", "track-3", "track-2"]
    want = ["file:///E:/Music/A/01.flac", "file:///E:/Music/A/03.flac",
            "file:///E:/Music/A/02.flac"]
    assert _wait(lambda: fake.playlist == want and b._mirror == "step", timeout=10), (
        fake.playlist, b._mirror)
    assert fake.state == int(PlaybackState.PLAYING) and fake.track == 1


def test_a_reconverge_whose_removal_hqplayer_left_unanswered_is_due_again(fake, output,
                                                                        monkeypatch):
    """The mirror lost through a silence, HQPlayer playing an entry of ours:
    its next answer gives it the queue back behind that entry. HQPlayer
    leaves that removal unanswered too, and carries it out once its silence
    is over: the reconverge is due again, and HQPlayer ends with the queue
    behind what it plays."""
    mgr, b = output                                  # playing slot 1 of 3
    mgr.queue.append([_item("E:/Music/A/04.flac", 4)])
    with fake._lock:
        fake.playlist = ["file:///E:/Music/A/01.flac", "file:///E:/Music/A/02.flac",
                         "file:///E:/Music/X/10.flac", "file:///E:/Music/A/03.flac"]
        fake.track, fake.state, fake.position = 2, int(PlaybackState.PLAYING), 10.0
    assert _wait(lambda: mgr._latest_status.get("track_index") == 2)
    _link().client.timeout = 0.5                      # a command's wait, shortened
    fake.service["PlaylistRemove"] = 1.0              # busy: the removal goes unanswered
    n = len(fake.commands)
    b.lose_mirror("HQPlayer took 0 of 1 appended tracks")
    try:
        assert _wait(lambda: any(c == "PlaylistRemove" for c, _ in fake.commands[n:]), timeout=8)
    finally:
        del fake.service["PlaylistRemove"]
        _link().client.timeout = hl.COMMAND_WAIT_S
    want = ["file:///E:/Music/A/01.flac", "file:///E:/Music/A/02.flac",
            "file:///E:/Music/A/03.flac", "file:///E:/Music/A/04.flac"]
    assert _wait(lambda: (b.poke(), fake.playlist == want)[1], timeout=15), (
        fake.playlist, b._mirror, b._reconverge_due)
    assert fake.state == int(PlaybackState.PLAYING) and fake.track == 2


def test_a_seamless_new_order_after_hqplayer_moved_on_never_stops_it(fake, output, monkeypatch):
    """In step: the new order is planned while HQPlayer reads slot 1, and
    HQPlayer moves on to track 2 by itself before the order goes. The entry it
    plays is never removed: the new order is the queue's, the mirror lost,
    and HQPlayer, playing on, gets what lies behind it at its next answer."""
    mgr, b = output
    player_router = _routes_on(monkeypatch, mgr)
    apply = mgr.apply_reorder

    def moved_on_first(plan):
        with fake._lock:                            # HQPlayer's own advance, not read yet
            fake.track, fake.position = 2, 0.0
        return apply(plan)
    monkeypatch.setattr(mgr, "apply_reorder", moved_on_first)
    said = player_router.reorder(player_router.ReorderRequest(
        order=["track-1", "track-3", "track-2"]))
    assert said["ok"] and said["interrupted"] is False, said
    assert [it.track_id for it in mgr.queue.snapshot()] == ["track-1", "track-3", "track-2"]
    assert fake.state == int(PlaybackState.PLAYING)
    assert fake.playlist[fake.track - 1] == "file:///E:/Music/A/02.flac"
    assert not [a for c, a in fake.commands if c == "PlaylistRemove"
                and a.get("index") == str(fake.track)], fake.commands


def test_a_rebuilt_new_order_after_hqplayer_moved_on_never_restarts_what_played(fake, output,
                                                                              monkeypatch):
    """In step, a new order that needs a rebuild is planned while slot 1 plays
    at 0:42; HQPlayer moves on to track 2 by itself before it goes. Nothing
    is rebuilt over the track it plays, nor the finished one started again:
    the new order is the queue's, the mirror lost, HQPlayer playing on."""
    mgr, b = output
    player_router = _routes_on(monkeypatch, mgr)
    apply = mgr.apply_reorder

    def moved_on_first(plan):
        with fake._lock:                            # HQPlayer's own advance, not read yet
            fake.track, fake.position = 2, 0.0
        return apply(plan)
    monkeypatch.setattr(mgr, "apply_reorder", moved_on_first)
    n = len(fake.commands)
    said = player_router.reorder(player_router.ReorderRequest(
        order=["track-2", "track-1", "track-3"]))
    assert said["ok"], said
    _settled()
    assert [it.track_id for it in mgr.queue.snapshot()] == ["track-2", "track-1", "track-3"]
    assert not [c for c, _ in fake.commands[n:] if c in ("Stop", "SelectTrack", "Seek")], [
        c for c, _ in fake.commands[n:] if c not in _QUESTIONS]
    assert fake.state == int(PlaybackState.PLAYING)
    assert fake.playlist[fake.track - 1] == "file:///E:/Music/A/02.flac"


def _moved_on_to_2_first(monkeypatch, fake, b, position=0.0):
    """HQPlayer moves on to track 2 by itself before a "play next" goes —
    after the status its anchor came from, not read yet."""
    insert = b.queue_insert_next

    def moved_on_first(items, anchor_index):
        with fake._lock:
            fake.track, fake.position = 2, position
        return insert(items, anchor_index)
    monkeypatch.setattr(b, "queue_insert_next", moved_on_first)
    return insert


def test_a_play_next_after_hqplayer_moved_on_goes_behind_what_it_plays(fake, output,
                                                                       monkeypatch):
    """In step, track 1 plays; a "play next" is anchored on the slot the last
    status named, and HQPlayer moves on to track 2 by itself before the
    insert goes: the track goes behind what HQPlayer reads — in HQPlayer and
    in the queue alike, the mirror in step — and the sound never stops."""
    mgr, b = output
    _moved_on_to_2_first(monkeypatch, fake, b)
    assert mgr.append([_item("E:/Music/N/01.flac", 31)], "next") == 1
    assert fake.state == int(PlaybackState.PLAYING)
    assert fake.playlist[fake.track - 1] == "file:///E:/Music/A/02.flac"
    assert fake.playlist[fake.track] == "file:///E:/Music/N/01.flac", fake.playlist
    assert [it.track_id for it in mgr.queue.snapshot()] == [
        "track-1", "track-2", "track-31", "track-3"]
    assert b._mirror == "step"


def test_a_removal_after_a_play_next_hqplayer_moved_on_from_never_stops_the_music(
        fake, output, monkeypatch):
    """The same "play next", then the person removes the new track from the
    queue sheet: in step the removal goes by its slot — the new track leaves
    HQPlayer, and what plays plays on."""
    mgr, b = output
    insert = _moved_on_to_2_first(monkeypatch, fake, b)
    assert mgr.append([_item("E:/Music/N/01.flac", 31)], "next") == 1
    monkeypatch.setattr(b, "queue_insert_next", insert)
    slot = next(i for i, it in enumerate(mgr.queue.snapshot(), 1) if it.track_id == "track-31")
    assert mgr.remove(slot, track_id="track-31")
    assert fake.state == int(PlaybackState.PLAYING), (fake.state, fake.track, fake.playlist)
    assert fake.playlist[fake.track - 1] == "file:///E:/Music/A/02.flac"
    assert "file:///E:/Music/N/01.flac" not in fake.playlist, fake.playlist


def test_a_tap_after_a_play_next_hqplayer_moved_on_from_plays_the_tapped_track(
        fake, output, monkeypatch):
    """The same "play next", then the person taps the new track's row: the
    new track plays — never the one HQPlayer had moved on to, started over."""
    mgr, b = output
    _moved_on_to_2_first(monkeypatch, fake, b, position=30.0)
    assert mgr.append([_item("E:/Music/N/01.flac", 31)], "next") == 1
    slot = next(i for i, it in enumerate(mgr.queue.snapshot(), 1) if it.track_id == "track-31")
    assert b.select(slot, "track-31")
    _settled()
    assert fake.playlist[fake.track - 1] == "file:///E:/Music/N/01.flac", (
        fake.track, fake.playlist)


def test_a_play_next_read_while_hqplayer_opens_the_next_entry_never_removes_it(
        fake, output, monkeypatch):
    """In step, track 1 plays; before a "play next" goes HQPlayer opens entry
    2 by itself — its Status says it plays and names no entry yet, which
    gives no verdict on what it reads: no reading. The entry it opens is
    never removed from under it; the insert is the queue's, the mirror
    lost."""
    mgr, b = output
    insert = b.queue_insert_next

    def opening_entry_2_first(items, anchor_index):
        with fake._lock:
            fake.track, fake.position, fake.opening = 2, 0.0, 50
        return insert(items, anchor_index)
    monkeypatch.setattr(b, "queue_insert_next", opening_entry_2_first)
    n = len(fake.commands)
    assert mgr.append([_item("E:/Music/N/01.flac", 31)], "next") == 1
    assert [c for c, _ in fake.commands[n:] if c == "PlaylistRemove"] == []
    assert fake.state == int(PlaybackState.PLAYING) and b._mirror == "lost", (
        fake.state, b._mirror, fake.playlist)


@pytest.mark.parametrize("order", [["track-1", "track-3", "track-2"],    # seamless
                                   ["track-2", "track-1", "track-3"]])   # a rebuild
def test_a_new_order_read_in_a_state_hqplayer_does_not_name_never_touches_what_it_starts(
        fake, output, monkeypatch, order):
    """In step, slot 1 plays at 0:42; before the new order goes HQPlayer is
    starting entry 2 by itself, its Status in a state the SDK does not name
    (Embedded said 5 while it started a play): no verdict on it — not
    stopped, not idle. The entry it starts is never removed, nor the
    finished track started again over it: the new order is the queue's, the
    mirror lost."""
    mgr, b = output
    player_router = _routes_on(monkeypatch, mgr)
    apply = mgr.apply_reorder

    def starting_entry_2_first(plan):
        with fake._lock:
            fake.state, fake.track, fake.position = 5, 2, 0.0
        return apply(plan)
    monkeypatch.setattr(mgr, "apply_reorder", starting_entry_2_first)
    n = len(fake.commands)
    player_router.reorder(player_router.ReorderRequest(order=order))
    _settled()
    sent = [(c, a.get("index")) for c, a in fake.commands[n:] if c not in _QUESTIONS]
    assert ("PlaylistRemove", "2") not in sent and "Stop" not in [c for c, _ in sent], sent
    assert [it.track_id for it in mgr.queue.snapshot()] == order


def test_a_rebuild_that_sends_nothing_leaves_no_failure_to_explain(fake, output, monkeypatch):
    """In step, a new order that needs a rebuild is planned while slot 1
    plays; HQPlayer moves on to track 2 before it goes, so the rebuild reads
    that in its sequence and sends nothing — HQPlayer plays on. Nothing
    failed: when the person then pauses, Now Playing offers no failed
    attempt to explain."""
    mgr, b = output
    player_router = _routes_on(monkeypatch, mgr)
    apply = mgr.apply_reorder

    def moved_on_first(plan):
        with fake._lock:
            fake.track, fake.position = 2, 0.0
        return apply(plan)
    monkeypatch.setattr(mgr, "apply_reorder", moved_on_first)
    assert player_router.reorder(player_router.ReorderRequest(
        order=["track-2", "track-1", "track-3"]))["ok"]
    _settled()
    assert _wait(lambda: (b.poke(), any(a.intent == "rebuild" for a in diag.attempts()))[1],
                 timeout=5), "the rebuild's attempt never closed"
    with fake._lock:
        fake.state = int(PlaybackState.PAUSED)
    assert _wait(lambda: mgr._latest_status.get("state") == "paused", timeout=5)
    assert mgr._latest_status.get("diagnosis") is None, mgr._latest_status.get("diagnosis")
    assert all(a.verdict["code"] != "unknown" for a in diag.attempts()), [
        (a.intent, a.end, a.verdict["code"]) for a in diag.attempts()]


@pytest.mark.parametrize("how", ["unanswered", "answered"])
def test_a_play_on_a_pause_whose_repair_hqplayer_left_unanswered_gets_the_queue_back(
        fake, output, monkeypatch, how):
    """A drifted playlist — [1, 2, x, 3] for the queue [1, 2, 3, 4] — HQPlayer
    paused on entry 2, ours. Play resumes it, and the repair behind the
    entry it reads begins. HQPlayer leaves the repair's first removal
    unanswered and carries it out once its silence is over: what it answers
    late it still does, so the playlist is lost through that silence and
    HQPlayer gets the queue behind what it plays back — never a drift,
    which nothing mends, over a playlist HQPlayer cut short meanwhile."""
    mgr, b = output                                  # playing slot 1 of 3
    mgr.queue.append([_item("E:/Music/A/04.flac", 4)])
    with fake._lock:
        fake.playlist = ["file:///E:/Music/A/01.flac", "file:///E:/Music/A/02.flac",
                         "file:///E:/Music/X/10.flac", "file:///E:/Music/A/03.flac"]
        fake.track, fake.state, fake.position = 2, int(PlaybackState.PAUSED), 10.0
    assert _wait(lambda: mgr._latest_status.get("state") == "paused"
                 and mgr._latest_status.get("track_index") == 2)
    b._drifted("an edit made outside this node")
    assert b._mirror == "drift"
    _link().client.timeout = 0.5                      # a command's wait, shortened
    fake.service["PlaylistRemove"] = 1.0 if how == "unanswered" else 0.0
    n = len(fake.commands)
    try:
        assert b.play()
        assert _wait(lambda: "file:///E:/Music/X/10.flac" not in fake.playlist, timeout=8)
    finally:
        del fake.service["PlaylistRemove"]
        _link().client.timeout = hl.COMMAND_WAIT_S
    want = ["file:///E:/Music/A/01.flac", "file:///E:/Music/A/02.flac",
            "file:///E:/Music/A/03.flac", "file:///E:/Music/A/04.flac"]
    assert _wait(lambda: (b.poke(), fake.playlist == want)[1], timeout=10), (
        fake.playlist, b._mirror, b._lost_by, b._reconverge_due)
    assert fake.state == int(PlaybackState.PLAYING) and fake.track == 2


@pytest.mark.parametrize("how", ["refused", "unanswered"])
def test_a_rebind_whose_playlist_hqplayer_did_not_give_loses_the_mirror(fake, how):
    """A playing HQPlayer; slot 3 re-bound to another copy. HQPlayer answers
    the convergence's Status but not its PlaylistGet: no reading is no empty
    playlist — the mirror is lost through a silence (HQPlayer gets the queue
    back at its next answer), never left "in step" over a file that left."""
    mgr = _album(4)
    b = _mirrored(mgr)
    fake.state, fake.track = int(PlaybackState.PLAYING), 1
    if how == "refused":
        fake.replies["PlaylistGet"] = '<PlaylistGet result="Error">busy</PlaylistGet>'
    else:
        _link().client.timeout = 0.5
        fake.service["PlaylistGet"] = 1.0
    try:
        _move(mgr, 3, "E:/Music/B/03.flac", 30)
        b.queue_changed("rebind")
    finally:
        fake.replies.pop("PlaylistGet", None)
        fake.service.pop("PlaylistGet", None)
        _link().client.timeout = hl.COMMAND_WAIT_S
    assert (b._mirror, b._lost_by) == ("lost", "silence"), (b._mirror, fake.playlist)


# -- A tap is resolved as its start goes --------------------------------------------------


def _tap_switched_out_after_its_check(monkeypatch, b, player_router, index, track_id,
                                      at=None):
    """A tap whose thread is switched out right after HqpBackend.select read
    that no mutation holds words (its first gap() call) — or at its first
    call of `at`, (an object, a method's name): returns (thread, the route's
    answer, release)."""
    entered, go = threading.Event(), threading.Event()
    owner, name = at or (b, "gap")
    method = getattr(owner, name)
    tapper, said = {}, {}

    def held(*args, **kwargs):
        if threading.current_thread() is tapper.get("t") and not entered.is_set():
            entered.set()
            go.wait(10)
        return method(*args, **kwargs)
    monkeypatch.setattr(owner, name, held)
    t = threading.Thread(target=lambda: said.update(player_router.jump(
        player_router.JumpRequest(index=index, track_id=track_id))), daemon=True)
    tapper["t"] = t
    t.start()
    assert entered.wait(10)
    return t, said, go


def _intents_watched(b):
    """The intent of every attempt the trace holds: closed, closing, watched."""
    with b._att_lock:
        pending = list(b._closing) + ([b._attempt] if b._attempt is not None else [])
    return [a.intent for a in diag.attempts() + pending]


def test_a_tap_as_a_play_next_begins_plays_the_tapped_track(fake, output, monkeypatch):
    """The person taps the third row (track 3) just as a "play next" begins:
    the tap found no mutation holding words, and is switched out before its
    start goes; the play next then holds words and moves HQPlayer's entries
    under the slot, and the tap's start comes before the queue commits the
    insert. Track 3 plays, never HQPlayer's entry 3 of the moved playlist —
    and the start carried out is the attempt watched: the held tap opens
    none of its own, which would be judged with nothing sent."""
    mgr, b = output
    player_router = _routes_on(monkeypatch, mgr)
    fake.service["PlaylistRemove"] = 0.4
    t, said, go = _tap_switched_out_after_its_check(monkeypatch, b, player_router, 3, "track-3")
    insert_after = mgr.queue.insert_after

    def once_the_tap_went(*args):
        t.join(10)                              # the queue commits after the tap's start
        return insert_after(*args)
    monkeypatch.setattr(mgr.queue, "insert_after", once_the_tap_went)
    n, came = len(fake.commands), len(fake.arrivals)
    inserting = threading.Thread(target=lambda: mgr.append(
        [_item("E:/Music/N/01.flac", 31)], "next"), daemon=True)
    inserting.start()
    assert _wait(lambda: b._inflight is not None
                 and "PlaylistRemove" in [c for _, c in fake.arrivals[came:]], timeout=5)
    go.set()
    t.join(10)
    inserting.join(10)
    _settled()
    assert said.get("ok"), said
    assert [it.track_id for it in mgr.queue.snapshot()] == [
        "track-1", "track-31", "track-2", "track-3"]
    assert fake.playlist[fake.track - 1] == "file:///E:/Music/A/03.flac", (
        said, fake.playlist, fake.track, _start_sent(fake, n))
    watched = _intents_watched(b)
    assert "select" not in watched and b._attempt.intent == "play", watched


def test_a_tap_across_a_removal_that_committed_plays_the_tapped_track(fake, output,
                                                                      monkeypatch):
    """The queue [1, 2, 3, 4], track 1 playing; the person taps track 3 and,
    while the tap is switched out after its check, track 2 is removed and the
    removal commits. Track 3 plays — never the third slot of the queue as it
    now stands."""
    mgr, b = output
    player_router = _routes_on(monkeypatch, mgr)
    assert mgr.append([_item("E:/Music/A/04.flac", 4)], "end") == 1
    assert fake.playlist[-1] == "file:///E:/Music/A/04.flac"
    t, said, go = _tap_switched_out_after_its_check(monkeypatch, b, player_router, 3, "track-3")
    n = len(fake.commands)
    assert mgr.remove(2, "track-2")
    go.set()
    t.join(10)
    _settled()
    assert said.get("ok"), said
    assert [it.track_id for it in mgr.queue.snapshot()] == ["track-1", "track-3", "track-4"]
    assert fake.playlist[fake.track - 1] == "file:///E:/Music/A/03.flac", (
        said, fake.playlist, fake.track, _start_sent(fake, n))


def test_a_tap_as_the_playlist_stops_being_a_mirror_is_a_play_press(fake, output, monkeypatch):
    """The queue [1, 2, 3], track 1 playing, in step; the person taps track 3
    and, while the tap waits to ask for the command lock, an entry is added
    in HQPlayer's own window ahead of it: the canary finds the playlist
    drifted. The tap goes as a play press of track 3 — never HQPlayer's
    entry 3, which names no slot of the queue any more."""
    mgr, b = output
    player_router = _routes_on(monkeypatch, mgr)
    monkeypatch.setattr(hb, "DRIFT_CHECK_EVERY", 1)              # the canary on every tick
    t, said, go = _tap_switched_out_after_its_check(monkeypatch, b, player_router, 3, "track-3",
                                                    at=(b._link, "command"))
    n = len(fake.commands)
    with fake._lock:
        fake.playlist.insert(1, "file:///Elsewhere/theirs.flac")
    assert _wait(lambda: b._mirror == "drift", timeout=5), b._mirror
    go.set()
    t.join(10)
    _settled()
    assert said.get("ok"), said
    assert fake.playlist[fake.track - 1] == "file:///E:/Music/A/03.flac", (
        said, fake.playlist, fake.track, _start_sent(fake, n))


def test_a_tap_on_a_renderer_while_a_removal_resyncs_names_its_track(monkeypatch):
    """A renderer (DLNA, the browser) plays the slot it is given and holds no
    words. Another device removes track 2 of [1, 2, 3, 4]; the removal has
    committed and the renderer re-syncs, the queue lock held. The person taps
    the third row of a sheet showing [1, 2, 3, 4] — track 3 — and track 3
    plays: the route hands such an output the slot that holds the track."""
    from routers import player as player_router
    mgr = PlaybackManager()
    mgr.queue.replace([_item(f"E:/Music/A/0{i}.flac", i) for i in (1, 2, 3, 4)])
    played = []
    resyncing, done = threading.Event(), threading.Event()

    class Renderer:
        id = "dlna"
        holds_words = False

        def select(self, index, track_id=None):           # as DlnaBackend.select
            item = mgr.queue.item_at(index)
            if item is None:
                return False
            played.append(item.track_id)
            return True

        def pressing(self):
            return None

        def queue_remove(self, index):
            return True

        def queue_changed(self, kind, *, play=False):     # its re-sync, on its loop
            resyncing.set()
            done.wait(5)

        @contextmanager
        def mutation(self):
            yield

        def queue_start(self):
            return None
    r = Renderer()
    mgr._active = r
    monkeypatch.setattr(player_router, "manager", mgr)
    monkeypatch.setattr(mgr, "ensure_active", lambda: r)
    t = threading.Thread(target=lambda: mgr.remove(2, "track-2"), daemon=True)
    t.start()
    assert resyncing.wait(5)
    said = player_router.jump(player_router.JumpRequest(index=3, track_id="track-3"))
    done.set()
    t.join(5)
    assert played == ["track-3"], (said, played)


def _a_tap_waiting_behind_a_slow_volume(b, link, fake):
    """A slow volume change holds the command lock, and a tap on the third
    row (track 3) — finding nothing in flight — waits its turn behind it:
    (the tap's thread, what it said, the volume's thread)."""
    fake.service["Volume"] = 1.5
    vol = threading.Thread(target=lambda: b.set_volume(-10), daemon=True)
    vol.start()
    assert _wait(lambda: link.lock.owner is not None, timeout=3)
    said = {}

    def tap():
        # HqpBackend.select as the jump route calls it (its play gate would
        # ask HQPlayer a question behind the volume first)
        try:
            said["ok"] = b.select(3, "track-3")
        except Exception as e:                  # a 409, as the route answers it
            said["error"] = e
    tapper = threading.Thread(target=tap, daemon=True)
    return tapper, said, vol


def test_a_tap_held_after_its_turn_never_overtakes_a_pause_made_after_it(fake, output):
    """Track 1 plays. A tap on the third row waits its turn behind a slow
    volume change; meanwhile a track is queued to play next — its hand-over
    holds the person's words — and the person presses Pause. Asked after the
    tap, the Pause is the newer word: the tap, held once it has the lock, is
    numbered as it was asked, and HQPlayer ends paused."""
    mgr, b = output
    link = _link()
    tapper, said, vol = _a_tap_waiting_behind_a_slow_volume(b, link, fake)
    tapper.start()
    assert _wait(lambda: len(link.lock._waiting) == 1, timeout=3)
    inserting = threading.Thread(target=lambda: mgr.append(
        [_item("E:/Music/N/01.flac", 31)], "next"), daemon=True)
    inserting.start()
    assert _wait(lambda: b._inflight is not None and len(link.lock._waiting) == 2, timeout=3)
    assert _quick(b.pause)
    tapper.join(10)
    inserting.join(10)
    vol.join(10)
    _settled()
    assert fake.state == int(PlaybackState.PAUSED), (said, fake.state, fake.track)


def test_a_tap_pressed_after_its_turn_never_overtakes_a_pause_made_after_it(fake, output,
                                                                             monkeypatch):
    """The same tap waits its turn; an entry is added in HQPlayer's own
    window (the canary finds the playlist drifted) and the person presses
    Pause, which waits behind the tap. Under the lock the tap finds no
    mirror and becomes a play press — numbered as it was asked, so the
    Pause stays the newer word: HQPlayer ends paused, never playing the
    tapped track."""
    mgr, b = output
    monkeypatch.setattr(hb, "DRIFT_CHECK_EVERY", 1)              # the canary on every tick
    link = _link()
    tapper, said, vol = _a_tap_waiting_behind_a_slow_volume(b, link, fake)
    attached = b._check_attached

    def once_drifted():                         # the tap's sequence, the lock its own
        if threading.current_thread() is tapper:
            assert _wait(lambda: b._mirror == "drift", timeout=10)
        return attached()
    monkeypatch.setattr(b, "_check_attached", once_drifted)
    tapper.start()
    assert _wait(lambda: len(link.lock._waiting) == 1, timeout=3)
    paused = {}
    pauser = threading.Thread(target=lambda: paused.update(ok=b.pause()), daemon=True)
    pauser.start()
    assert _wait(lambda: len(link.lock._waiting) == 2, timeout=3)
    with fake._lock:
        fake.playlist.insert(1, "file:///Elsewhere/theirs.flac")
    tapper.join(10)
    pauser.join(10)
    vol.join(10)
    _settled()
    assert fake.state == int(PlaybackState.PAUSED), (said, paused, fake.state, fake.track)


def test_a_tap_refused_under_the_lock_leaves_the_new_queues_start(fake, output, monkeypatch):
    """The person taps the third row (track 3) just as album B — which does
    not hold it — begins to load: the tap found no mutation holding words
    and is switched out after its check; album B's hand-over then holds
    words, and the tap, decided again under the lock, is refused. A tap
    refused there supersedes nothing — the words before stand: album B
    plays from its first track."""
    mgr, b = output
    player_router = _routes_on(monkeypatch, mgr)
    n = _slow_adds(fake)
    t, said, go = _tap_switched_out_after_its_check(monkeypatch, b, player_router, 3, "track-3")
    replace = mgr.queue.replace

    def once_the_tap_was_answered(items):
        t.join(10)                              # the queue commits after the tap's answer
        return replace(items)
    monkeypatch.setattr(mgr.queue, "replace", once_the_tap_was_answered)
    loading = _loading(mgr, _b_album(), play=True)
    _handing_over(fake, n)
    assert b._inflight is not None
    go.set()
    loading.join(15)
    t.join(10)
    _settled()
    assert said.get("ok") is False and "being loaded" in (said.get("refusal") or ""), said
    assert [it.track_id for it in mgr.queue.snapshot()] == ["track-11", "track-12", "track-13"]
    assert _wait(lambda: fake.state == int(PlaybackState.PLAYING)
                 and fake.playlist[fake.track - 1].endswith("B/01.flac"), timeout=5), (
        fake.state, fake.track, _start_sent(fake, n))


def test_a_tap_across_a_new_queue_that_committed_plays_its_track(fake, output, monkeypatch):
    """The person taps the third row (track 3) as the same album is started
    again — new slots, the same tracks: the tap is decided under the lock
    once the new queue committed, and finds its track there by the track,
    as its first check does: track 3 plays."""
    mgr, b = output
    player_router = _routes_on(monkeypatch, mgr)
    n = _slow_adds(fake)
    t, said, go = _tap_switched_out_after_its_check(monkeypatch, b, player_router, 3, "track-3")
    gen0 = mgr.queue.generation
    cmd = b._cmd

    def after_the_commit(func, *args, **kw):
        if threading.current_thread() is t:
            assert _wait(lambda: b._inflight is None and mgr.queue.generation != gen0,
                         timeout=10)
        return cmd(func, *args, **kw)
    monkeypatch.setattr(b, "_cmd", after_the_commit)
    loading = _loading(mgr, [_item(f"E:/Music/A/0{i}.flac", i) for i in (1, 2, 3)], play=True)
    _handing_over(fake, n)
    assert b._inflight is not None
    go.set()
    loading.join(15)
    t.join(10)
    _settled()
    assert said.get("ok") and _wait(
        lambda: fake.state == int(PlaybackState.PLAYING)
        and fake.playlist[fake.track - 1].endswith("A/03.flac"), timeout=5), (
        said, fake.state, fake.track, _start_sent(fake, n))


def test_a_play_then_a_seek_held_by_a_clear_that_lost_the_mirror_start_at_the_seek(fake, output):
    """HQPlayer stopped on track 1; a radio begins (its clear reads no entry
    to keep: the mirror is lost) and the person presses Play, then drags the
    position to 60 s while the clear is on its way. Once the queue stands the
    Play is a play press — HQPlayer takes the queue afresh — and the Seek
    moves its start, as the same two words do outside a mutation: the start
    goes at 60 s."""
    mgr, b = output
    b.stop()
    _stopped(mgr, fake)
    fake.service["PlaylistClear"] = 0.6
    came = len(fake.arrivals)
    radio = threading.Thread(target=lambda: mgr.clear_for_radio(lambda: None), daemon=True)
    radio.start()
    assert _wait(lambda: b._inflight is not None
                 and "PlaylistClear" in [c for _, c in fake.arrivals[came:]], timeout=5)
    n = len(fake.commands)
    assert _quick(b.play)
    assert _quick(lambda: b.seek(60))
    radio.join(10)
    _settled()
    sent = [(c, a) for c, a in fake.commands[n:] if c not in _QUESTIONS]
    selects = [i for i, (c, _) in enumerate(sent) if c == "SelectTrack"]
    assert selects, sent
    assert ("Seek", {"position": "60"}) in sent[selects[-1]:], sent


def _a_play_next_hqplayer_refuses_whole(mgr, fake, out) -> threading.Thread:
    """A track queued to play next that HQPlayer refuses whole — the queue
    stays as it was — its hand-over under way."""
    fake.service["PlaylistRemove"] = 0.4
    fake.refuse.add("N/01.flac")
    came = len(fake.arrivals)
    inserting = threading.Thread(target=lambda: out.update(added=mgr.append(
        [_item("E:/Music/N/01.flac", 31)], "next")), daemon=True)
    inserting.start()
    assert _wait(lambda: "PlaylistRemove" in [c for _, c in fake.arrivals[came:]], timeout=5)
    return inserting


def test_next_twice_held_by_a_play_next_hqplayer_refused_whole_steps_twice(fake, output,
                                                                           monkeypatch):
    """Track 1 of [1, 2, 3, 4] plays; a "play next" HQPlayer refuses whole
    leaves the queue as it was. Next pressed twice meanwhile counts on the
    queue as it stands, from what HQPlayer plays: track 3."""
    mgr, b = output
    player_router = _routes_on(monkeypatch, mgr)
    assert mgr.append([_item("E:/Music/A/04.flac", 4)], "end") == 1
    out = {}
    inserting = _a_play_next_hqplayer_refuses_whole(mgr, fake, out)
    assert b._inflight is not None
    assert _quick(player_router.next_track)["ok"]
    assert _quick(player_router.next_track)["ok"]
    inserting.join(10)
    _settled()
    assert out.get("added") == 0, out
    assert _wait(lambda: fake.state == int(PlaybackState.PLAYING)
                 and fake.playlist[fake.track - 1] == "file:///E:/Music/A/03.flac"), (
        fake.state, fake.track, fake.playlist)


def test_next_then_play_held_by_a_play_next_hqplayer_refused_whole_plays_the_next(
        fake, output, monkeypatch):
    """Track 1 of [1, 2, 3] plays; a "play next" HQPlayer refuses whole.
    Next, then Play, made meanwhile: the Play keeps the Next's count, which
    counts on the queue as it stands — track 2 plays."""
    mgr, b = output
    player_router = _routes_on(monkeypatch, mgr)
    out = {}
    inserting = _a_play_next_hqplayer_refuses_whole(mgr, fake, out)
    assert b._inflight is not None
    assert _quick(player_router.next_track)["ok"]
    assert _quick(player_router.play)["ok"]
    inserting.join(10)
    _settled()
    assert out.get("added") == 0, out
    assert _wait(lambda: fake.state == int(PlaybackState.PLAYING)
                 and fake.playlist[fake.track - 1] == "file:///E:/Music/A/02.flac"), (
        fake.state, fake.track, fake.playlist)


def test_a_next_held_by_a_removal_hqplayer_refused_counts_on_the_queue_as_it_stands(
        fake, output, monkeypatch):
    """Track 1 of [1, 2, 3] plays; the person removes track 2 and HQPlayer
    refuses the removal — the slot stays. Next pressed while HQPlayer was
    asked counts on the queue as it stands, from what HQPlayer plays: track
    2, never the track after it."""
    from playback.base import OutputRefused
    mgr, b = output
    player_router = _routes_on(monkeypatch, mgr)
    fake.service["PlaylistRemove"] = 0.6
    fake.replies["PlaylistRemove"] = '<PlaylistRemove result="Error">Not now</PlaylistRemove>'
    came, out = len(fake.arrivals), {}

    def removing():
        try:
            out["ok"] = mgr.remove(2, "track-2")
        except OutputRefused as e:
            out["error"] = e
    r = threading.Thread(target=removing, daemon=True)
    r.start()
    assert _wait(lambda: "PlaylistRemove" in [c for _, c in fake.arrivals[came:]], timeout=5)
    assert b._inflight is not None
    assert _quick(player_router.next_track)["ok"]
    r.join(10)
    _settled()
    fake.replies.pop("PlaylistRemove", None)
    assert isinstance(out.get("error"), OutputRefused), out
    assert [it.track_id for it in mgr.queue.snapshot()] == ["track-1", "track-2", "track-3"]
    assert _wait(lambda: fake.state == int(PlaybackState.PLAYING)
                 and fake.playlist[fake.track - 1] == "file:///E:/Music/A/02.flac"), (
        fake.state, fake.track, fake.playlist)


def test_next_then_a_seek_held_by_a_play_next_hqplayer_refused_whole_starts_at_the_seek(
        fake, output, monkeypatch):
    """... and Next, then a Seek to 0:30: the Seek keeps the Next's count on
    the queue as it stands — track 2 plays, from 0:30."""
    mgr, b = output
    player_router = _routes_on(monkeypatch, mgr)
    out = {}
    inserting = _a_play_next_hqplayer_refuses_whole(mgr, fake, out)
    assert b._inflight is not None
    n = len(fake.commands)
    assert _quick(player_router.next_track)["ok"]
    assert _quick(lambda: b.seek(30))
    inserting.join(10)
    _settled()
    assert out.get("added") == 0, out
    assert _wait(lambda: fake.state == int(PlaybackState.PLAYING)
                 and fake.playlist[fake.track - 1] == "file:///E:/Music/A/02.flac"), (
        fake.state, fake.track, fake.playlist)
    sent = _start_sent(fake, n)
    assert ("Seek", {"position": "30"}) in sent, sent


def test_a_play_next_read_on_an_entry_not_the_queues_never_removes_it(fake, output,
                                                                       monkeypatch):
    """In step, track 1 plays; before a "play next" goes, another controller
    adds an entry and HQPlayer plays it — no tick has read it yet. The
    insert's own read names no slot of the queue's: the insert is the
    queue's, the mirror lost, and the entry HQPlayer plays is never removed
    from under it."""
    mgr, b = output
    insert = b.queue_insert_next

    def theirs_first(items, anchor_index):
        with fake._lock:
            fake.playlist.append("file:///Elsewhere/theirs.flac")
            fake.track, fake.position = 4, 0.0
        return insert(items, anchor_index)
    monkeypatch.setattr(b, "queue_insert_next", theirs_first)
    n = len(fake.commands)
    assert mgr.append([_item("E:/Music/N/01.flac", 31)], "next") == 1
    removed = [a.get("index") for c, a in fake.commands[n:] if c == "PlaylistRemove"]
    assert removed == [] and b._mirror == "lost", (removed, b._mirror, fake.playlist)
    assert fake.state == int(PlaybackState.PLAYING)
    assert fake.playlist[fake.track - 1] == "file:///Elsewhere/theirs.flac", fake.playlist


def test_a_play_next_read_in_a_state_hqplayer_does_not_name_edits_nothing(fake, output,
                                                                          monkeypatch):
    """In step, track 1 plays; before a "play next" goes, HQPlayer's Status
    says a state the SDK does not name on entry 1 (Embedded said 5 while it
    started a play): no reading — nothing is removed, the insert is the
    queue's and the mirror lost."""
    mgr, b = output
    insert = b.queue_insert_next

    def unnamed_first(items, anchor_index):
        with fake._lock:
            fake.state = 5
        return insert(items, anchor_index)
    monkeypatch.setattr(b, "queue_insert_next", unnamed_first)
    n = len(fake.commands)
    assert mgr.append([_item("E:/Music/N/01.flac", 31)], "next") == 1
    removed = [a.get("index") for c, a in fake.commands[n:] if c == "PlaylistRemove"]
    assert removed == [] and b._mirror == "lost", (removed, b._mirror, fake.playlist)


def test_a_next_held_by_a_play_next_hqplayer_took_after_moving_on_plays_the_new_track(
        fake, output, monkeypatch):
    """In step, track 1 of [1, 2, 3] plays. A "play next" is anchored on the
    slot the last status named, and HQPlayer moves on to track 2 by itself
    before the insert goes: the new track goes behind track 2, in HQPlayer
    and in the queue alike. The person presses Next while HQPlayer takes
    the insert — the route answers that the new track starts. Next from
    what plays is the new track; never track 2, the one HQPlayer plays,
    started over."""
    mgr, b = output
    player_router = _routes_on(monkeypatch, mgr)
    fake.service["PlaylistRemove"] = 0.4
    insert = b.queue_insert_next

    def moved_on_first(items, anchor_index):
        with fake._lock:                    # HQPlayer's own advance, not read yet
            fake.track, fake.position = 2, 30.0
        return insert(items, anchor_index)
    monkeypatch.setattr(b, "queue_insert_next", moved_on_first)
    came, out = len(fake.arrivals), {}
    inserting = threading.Thread(target=lambda: out.update(added=mgr.append(
        [_item("E:/Music/N/01.flac", 31)], "next")), daemon=True)
    inserting.start()
    assert _wait(lambda: "PlaylistRemove" in [c for _, c in fake.arrivals[came:]], timeout=5)
    assert b._inflight is not None
    n = len(fake.commands)
    said = _quick(player_router.next_track)
    inserting.join(10)
    _settled()
    assert out.get("added") == 1, out
    assert [it.track_id for it in mgr.queue.snapshot()] == [
        "track-1", "track-2", "track-31", "track-3"]
    assert said.get("starts") == {"index": 3, "position": 0, "track_id": "track-31"}, said
    sent = _start_sent(fake, n)
    assert ("SelectTrack", {"index": "2"}) not in sent, (said, sent, fake.track, fake.position)
    assert _wait(lambda: fake.state == int(PlaybackState.PLAYING)
                 and fake.playlist[fake.track - 1] == "file:///E:/Music/N/01.flac"), (
        said, sent, fake.track, fake.playlist)


def test_a_held_seek_never_moves_a_start_made_after_it(fake, output, monkeypatch):
    """HQPlayer stopped on track 1; a radio's clear loses the mirror; Play,
    then Seek 60, held meanwhile. Once the queue stands the Play becomes a
    play press and the Seek moves its start — but in the moment between the
    carry-out's check of the Seek and its move the person taps the seed's
    row (track 1, from its start: a start of their own on the press, newer
    than the Seek). The Seek, older, never gives the tap its second: the
    tapped track starts from 0 s."""
    mgr, b = output
    b.stop()
    _stopped(mgr, fake)
    fake.service["PlaylistClear"] = 0.6
    fake.service["PlaylistAdd"] = 0.3       # the press's hand-over takes a while
    came = len(fake.arrivals)
    seek_pressed = b._seek_pressed
    tapped = {}

    def tap_lands_first(seconds, *, word=None):
        if word is not None and not tapped:
            t = threading.Thread(target=lambda: tapped.update(ok=b.select(1, "track-1")),
                                 daemon=True)
            t.start()
            t.join(5)
        return seek_pressed(seconds, word=word)
    monkeypatch.setattr(b, "_seek_pressed", tap_lands_first)
    radio = threading.Thread(target=lambda: mgr.clear_for_radio(lambda: None), daemon=True)
    radio.start()
    assert _wait(lambda: b._inflight is not None
                 and "PlaylistClear" in [c for _, c in fake.arrivals[came:]], timeout=5)
    n = len(fake.commands)
    assert _quick(b.play)
    assert _quick(lambda: b.seek(60))
    radio.join(10)
    _settled()
    sent = _start_sent(fake, n)
    assert tapped.get("ok"), tapped
    selects = [i for i, (c, _) in enumerate(sent) if c == "SelectTrack"]
    assert selects, sent
    assert ("Seek", {"position": "60"}) not in sent[selects[-1]:], (sent, fake.track,
                                                                    fake.position)


def _a_play_next_holding_words_behind(mgr, b, link):
    """A track queued to play next while the tap waits its turn: its
    hand-over holds the person's words, its sequence behind the tap's."""
    inserting = threading.Thread(target=lambda: mgr.append(
        [_item("E:/Music/N/01.flac", 31)], "next"), daemon=True)
    inserting.start()
    assert _wait(lambda: b._inflight is not None and len(link.lock._waiting) == 2, timeout=3)
    return inserting


def _shows_what_plays(mgr, b, fake) -> None:
    """The tap that yielded leaves no trace: the status — the queue sheet's
    active row, Now Playing — names the track HQPlayer plays, never the
    tapped one, and no play press is said to be under way."""
    def shown():
        s = mgr._latest_status
        item = mgr.queue.item_at(s.get("track_index") or 0)
        with fake._lock:
            reads = (fake.playlist[fake.track - 1]
                     if 1 <= fake.track <= len(fake.playlist) else None)
        return (s.get("state") == "playing" and item is not None
                and item.track_id != "track-3" and b._uri_of(item)[0] == reads
                and not s.get("rebuilding"))
    assert _wait(shown, timeout=5), (mgr._latest_status, fake.track, fake.playlist)


def test_a_play_made_while_a_tap_waited_its_turn_plays(fake, output):
    """HQPlayer is paused on track 1. A tap on the third row waits its turn
    behind a slow volume change; a track is queued to play next, and the
    person presses Play — made on what played, without the tap. The tap,
    the older word, yields: the Play goes, HQPlayer plays, and the screen
    shows what it plays — never the tapped track."""
    mgr, b = output
    assert b.pause()
    assert _wait(lambda: mgr._latest_status.get("state") == "paused", timeout=5)
    link = _link()
    tapper, said, vol = _a_tap_waiting_behind_a_slow_volume(b, link, fake)
    tapper.start()
    assert _wait(lambda: len(link.lock._waiting) == 1, timeout=3)
    inserting = _a_play_next_holding_words_behind(mgr, b, link)
    n = len(fake.commands)
    assert _quick(b.play)
    tapper.join(10)
    inserting.join(10)
    vol.join(10)
    _settled()
    sent = [(c, a) for c, a in fake.commands[n:] if c not in _QUESTIONS]
    assert fake.state == int(PlaybackState.PLAYING), (said, fake.state, fake.track, sent)
    _shows_what_plays(mgr, b, fake)


def test_a_seek_made_while_a_tap_waited_its_turn_goes(fake, output):
    """Track 1 plays. The same tap waits its turn; a track is queued to play
    next and the person drags the position to 60 s — on what plays. The tap
    yields: the Seek goes, never dropped with the tap, and the screen shows
    what plays."""
    mgr, b = output
    link = _link()
    tapper, said, vol = _a_tap_waiting_behind_a_slow_volume(b, link, fake)
    tapper.start()
    assert _wait(lambda: len(link.lock._waiting) == 1, timeout=3)
    inserting = _a_play_next_holding_words_behind(mgr, b, link)
    n = len(fake.commands)
    assert _quick(lambda: b.seek(60))
    tapper.join(10)
    inserting.join(10)
    vol.join(10)
    _settled()
    sent = [(c, a) for c, a in fake.commands[n:] if c not in _QUESTIONS]
    assert ("Seek", {"position": "60"}) in sent, (said, fake.state, fake.track, sent)
    _shows_what_plays(mgr, b, fake)


@pytest.mark.parametrize("word", ["seek", "play"])
def test_a_word_made_while_a_tap_waited_never_leaves_its_press_stopped(fake, output, monkeypatch,
                                                                       word):
    """The same tap waits its turn; an entry is added in HQPlayer's own
    window (the canary finds the playlist drifted) and the person seeks, or
    — HQPlayer paused — presses Play, a word that waits behind the tap.
    Under the lock the tap finds no mirror and becomes a play press; the
    word goes first, on what played. The press, its start dead before it
    began, rebuilds nothing: the music plays, and the screen shows it."""
    mgr, b = output
    if word == "play":
        assert b.pause()
        assert _wait(lambda: mgr._latest_status.get("state") == "paused", timeout=5)
    monkeypatch.setattr(hb, "DRIFT_CHECK_EVERY", 1)
    link = _link()
    tapper, said, vol = _a_tap_waiting_behind_a_slow_volume(b, link, fake)
    attached = b._check_attached

    def once_drifted():
        if threading.current_thread() is tapper:
            assert _wait(lambda: b._mirror == "drift", timeout=10)
        return attached()
    monkeypatch.setattr(b, "_check_attached", once_drifted)
    tapper.start()
    assert _wait(lambda: len(link.lock._waiting) == 1, timeout=3)
    done = {}
    act = (lambda: b.seek(60)) if word == "seek" else b.play
    actor = threading.Thread(target=lambda: done.update(ok=act()), daemon=True)
    actor.start()
    assert _wait(lambda: len(link.lock._waiting) == 2, timeout=3)
    with fake._lock:
        fake.playlist.insert(1, "file:///Elsewhere/theirs.flac")
    n = len(fake.commands)
    tapper.join(10)
    actor.join(10)
    vol.join(10)
    _settled()
    sent = [(c, a.get("index") or a.get("uri") or a.get("position")) for c, a in fake.commands[n:]
            if c not in _QUESTIONS]
    assert fake.state == int(PlaybackState.PLAYING), (said, done, fake.state, fake.track,
                                                      b._spot, sent)
    assert "Stop" not in [c for c, _ in sent], sent
    _shows_what_plays(mgr, b, fake)


def test_a_pause_during_a_play_press_still_hands_hqplayer_the_queue(fake, output):
    """A Pause made while a play press waits to begin halts it: the press
    rebuilds all the same — HQPlayer takes the queue — and nothing starts,
    HQPlayer stopped at the track the press was to start. Only a press none
    of whose starts is live, holding no Pause or Stop, rebuilds nothing."""
    mgr, b = output
    b.lose_mirror("HQPlayer took 0 of 1 appended tracks", by="refusal")
    fake.commands.clear()
    with mgr._mutate_lock:                           # a queue edit still committing
        assert b.select(3)                           # a tap: the press, waiting to begin
        assert b.pause()
    _settled()
    assert "PlaylistAdd" in [c for c, _ in fake.commands], fake.commands
    assert fake.state == int(PlaybackState.STOPPED)
    assert _wait(lambda: mgr._latest_status.get("resume") == {"index": 3, "position": 0})


def _a_stop_answered_then_silent(monkeypatch, fake, link) -> None:
    """A Stop HQPlayer answers at once and then falls silent past its wait:
    the connection passes to the link's held Status, the command lock free."""
    monkeypatch.setattr(hl, "STOP_ANSWER_S", 0.6)
    monkeypatch.setattr(hl.BuildWatch, "after", 0.3)
    link.client.waits.update(hl._command_waits())
    real = hl.HqpLink._end_scope

    def held_up(self):
        if threading.current_thread().name == "hqp-held-status":
            time.sleep(0.2)
        return real(self)
    monkeypatch.setattr(hl.HqpLink, "_end_scope", held_up)
    fake.build("Stop", 3.0, answer="first")                    # answered at once, then silent
    with link.command():
        assert link.client.stop()
    assert _wait(lambda: [c for _, c in fake.arrivals][-2:] == ["Stop", "Status"], timeout=3)


def test_a_command_refused_at_its_first_request_keeps_its_number(fake, monkeypatch):
    """A Play takes the free command lock and is refused at its first
    request — HQPlayer is not answering — with nothing sent. It held the
    lock: its number stands, and a start owed from before is superseded by
    it, never sent over the person's newer word."""
    link = _link()
    older = link.item("start", link.accept("transport"))       # a start owed from before
    _a_stop_answered_then_silent(monkeypatch, fake, link)
    with pytest.raises(hl.NotAnswering):
        with link.command("transport"):
            link.client.play()
    assert "Play" not in [c for _, c in fake.arrivals]
    assert link.superseded(older) is not None


def test_a_pause_refused_at_its_first_request_says_it_did_not_go(fake, output, monkeypatch):
    """The same silence behind the link's held Status, and the person
    presses Pause: the route answers that it did not go, in HQPlayer's
    state's words — never an ok over a Pause that was not sent."""
    from fastapi import HTTPException
    mgr, b = output
    player_router = _routes_on(monkeypatch, mgr)
    link = b._link
    _a_stop_answered_then_silent(monkeypatch, fake, link)
    with pytest.raises(HTTPException) as e:
        player_router.pause()
    assert e.value.status_code == 409, e.value.detail
    assert e.value.detail == hl.NOT_ANSWERING, e.value.detail
    assert "Pause" not in [c for _, c in fake.arrivals]
