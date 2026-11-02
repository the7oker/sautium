"""What the media proxy holds for a connection it does not know (streaming/
proxy.py, transcode.py): a request line and headers within a read timeout,
an answer that waits for its consumer as long as it takes, so many
connections at once, a record of requests for tokens it does not hold kept
apart and cut short, a span served from the buffer itself, so many encodes
at once with a fixed set of locks, and a token named in a log by its prefix."""

import hashlib
import logging
import socket
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent.parent / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from streaming import proxy as proxy_mod  # noqa: E402
from streaming import transcode  # noqa: E402
from streaming.base import FetchedAudio, ProviderManifest, StreamProvider, TrackQuery  # noqa: E402
from streaming.proxy import MediaProxy  # noqa: E402


@pytest.fixture
def started():
    """A started proxy on loopback, and its port; shut down after."""
    servers = []

    def start():
        p = MediaProxy(port=0, advertised_host="127.0.0.1", file_token_key=b"k", bind_host="127.0.0.1")
        p.start()
        servers.append(p)
        return p, p._httpd.server_address[1]
    yield start
    for p in servers:
        p._httpd.shutdown()
        p._httpd.server_close()


def _idle(port: int) -> socket.socket:
    s = socket.create_connection(("127.0.0.1", port))
    s.settimeout(3)
    return s


def _closed_within(s: socket.socket, seconds: float) -> bool:
    s.settimeout(seconds)
    try:
        return s.recv(1) == b""
    except (socket.timeout, TimeoutError):
        return False
    except ConnectionResetError:
        return True


def test_a_request_that_does_not_arrive_is_let_go_and_an_answer_waits_for_its_reader(
        started, tmp_path, monkeypatch):
    monkeypatch.setattr(proxy_mod, "_REQUEST_READ_S", 0.3)
    proxy, port = started()
    assert _closed_within(_idle(port), 2.0)
    # once the request is in, the answer waits as long as its reader does
    track = tmp_path / "t.flac"
    track.write_bytes(b"y" * (4 << 20))
    tok = proxy.register_file(str(track), "audio/flac")
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/file/{tok}", timeout=5) as r:
        first = r.read(1024)
        time.sleep(1.0)
        assert len(first) + len(r.read()) == 4 << 20


def test_connections_past_the_cap_are_closed_as_they_arrive(started, monkeypatch):
    monkeypatch.setattr(proxy_mod, "_CONNECTIONS", 3)
    monkeypatch.setattr(proxy_mod, "_REQUEST_READ_S", 5.0)
    _proxy, port = started()
    held = [_idle(port) for _ in range(3)]
    time.sleep(0.2)
    assert _closed_within(_idle(port), 1.0)
    for s in held:
        s.close()
    time.sleep(0.3)
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(f"http://127.0.0.1:{port}/file/unknown", timeout=3)
    assert e.value.code == 404


def test_requests_for_tokens_the_proxy_does_not_hold_push_no_live_record_out(started, tmp_path):
    proxy, port = started()
    track = tmp_path / "t.flac"
    track.write_bytes(b"z" * 1000)
    tok = proxy.register_file(str(track), "audio/flac")
    urllib.request.urlopen(f"http://127.0.0.1:{port}/file/{tok}", timeout=5).read()
    for i in range(proxy_mod._HIT_TOKENS + 20):
        with pytest.raises(urllib.error.HTTPError):
            urllib.request.urlopen(f"http://127.0.0.1:{port}/file/stray{i}{'x' * 200}", timeout=5)
    assert [h["status"] for h in proxy.hits(tok)] == [200]
    assert len(proxy._stray_hits) <= proxy_mod._STRAY_TOKENS
    assert max(len(t) for t in proxy._stray_hits) <= proxy_mod._TOKEN_KEPT
    assert proxy.hits(f"stray{proxy_mod._HIT_TOKENS + 19}{'x' * 200}")[-1]["status"] == 404


class _Instant(StreamProvider):
    manifest = ProviderManifest(id="stub", name="Stub", kind="direct_url", lossless=True)

    def fetch(self, query):
        return FetchedAudio(data=bytes(range(256)) * 4096, mime="audio/flac", lossless=True)


def test_a_span_of_a_buffer_is_sent_from_the_buffer_itself(started):
    proxy, port = started()
    tok = proxy.start_session([(TrackQuery(artist="A", title="t", album="B", duration=60.0,
                                           track_id="id-t"), [(_Instant(), None)])])[0]
    proxy.wait_ready(tok, timeout=5)
    handler = proxy._httpd.RequestHandlerClass
    written = []
    real = handler._write

    def recording(self, data):
        written.append(type(data))
        return real(self, data)
    handler._write = recording
    req = urllib.request.Request(f"http://127.0.0.1:{port}/preview/{tok}",
                                 headers={"Range": "bytes=1000-200999"})
    with urllib.request.urlopen(req, timeout=5) as r:
        assert r.status == 206 and len(r.read()) == 200000
    assert written == [memoryview]


def test_encodes_run_so_many_at_once_with_a_fixed_set_of_locks(tmp_path, monkeypatch):
    monkeypatch.setattr(transcode, "_CACHE_DIR", tmp_path)
    running, peak, guard = [0], [0], threading.Lock()

    def encode(args_in, out_args, out_path, stdin_bytes):
        with guard:
            running[0] += 1
            peak[0] = max(peak[0], running[0])
        time.sleep(0.1)
        out_path.write_bytes(b"opus")
        with guard:
            running[0] -= 1
    monkeypatch.setattr(transcode, "run_ffmpeg", encode)
    locks = len(transcode._key_locks)
    keys = [hashlib.sha1(f"track {i}".encode()).hexdigest() for i in range(208)]    # as _produce's callers make them
    threads = [threading.Thread(target=transcode._produce, args=(keys[i],),
                                kwargs=dict(suffix=".ogg", label="t", args_in=[], out_args=[],
                                            stdin_bytes=None)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
    assert peak[0] == transcode.ENCODES
    for key in keys[8:]:
        transcode._key_lock(key)
    assert len(transcode._key_locks) == locks


def test_a_token_is_named_in_the_log_by_its_prefix(started, caplog):
    proxy, port = started()
    tok = proxy.register_file("/nowhere/at/all.flac", "audio/flac")
    caplog.set_level(logging.ERROR, logger=proxy_mod.__name__)
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(f"http://127.0.0.1:{port}/file/{tok}", timeout=5)
    assert e.value.code == 500
    logged = " ".join(r.getMessage() for r in caplog.records)
    assert proxy_mod.shown(tok) in logged and tok not in logged
