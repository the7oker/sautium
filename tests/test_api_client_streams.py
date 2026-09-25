"""close_streams() ends a blocked SSE read at once (desktop/api_client.py).

The launcher calls it on the Tk thread when quitting and before an update
relaunch. Closing the response from there waited for the server's next
keepalive — up to 20 s of a frozen window (2026-09-25) — so it shuts the
reader's socket down instead. A real HTTP server on loopback whose next
keepalive is 30 s away."""

import http.server
import threading
import time

import pytest

from desktop import api_client
from desktop.api_client import BackendAPIClient

KEEPALIVE_S = 30


class _Events(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        self.wfile.write(b"data: {}\n\n")
        self.wfile.flush()
        self.server.gone.wait(KEEPALIVE_S)

    def log_message(self, *args):
        pass


@pytest.fixture
def server(monkeypatch):
    monkeypatch.setattr(api_client, "_cached_secret", b"test-secret")
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Events)
    srv.gone = threading.Event()
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv
    srv.gone.set()
    srv.shutdown()
    srv.server_close()


def test_close_streams_ends_a_blocked_read_at_once(server):
    client = BackendAPIClient(base_url=f"http://127.0.0.1:{server.server_port}")
    first_event = threading.Event()

    def read():
        for _ in client.stream("/api/events"):
            first_event.set()

    reader = threading.Thread(target=read, daemon=True)
    reader.start()
    # The reader has its event; its next step is the read that blocks.
    assert first_event.wait(5)

    started = time.monotonic()
    client.close_streams()
    call_took = time.monotonic() - started
    reader.join(5)

    assert call_took < 1
    assert not reader.is_alive()
    assert time.monotonic() - started < 2
    assert client._streams == []
