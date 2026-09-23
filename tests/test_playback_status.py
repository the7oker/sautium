"""Playback status plumbing: the manager hears the ACTIVE backend only, the
DLNA liveness probe is decided by the first address that answers, and a
failure on our own disk is never read as the renderer going away."""

import asyncio
import errno
import socket
import sys
import time
import types
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from playback.base import Capabilities, PlaybackStatus, PlayerBackend  # noqa: E402
from async_upnp_client.exceptions import (  # noqa: E402
    UpnpConnectionError, UpnpConnectionTimeoutError)
from playback.dlna_backend import DlnaBackend, renderer_online  # noqa: E402
from playback.manager import PlaybackManager  # noqa: E402
from playback.queue import CanonicalQueue, QueueItem  # noqa: E402
from streaming import service as streaming_service  # noqa: E402
from streaming.proxy import MediaProxy  # noqa: E402


class _Stub(PlayerBackend):
    id = "stub"
    label = "stub"

    def start(self): pass
    def shutdown(self): pass
    def capabilities(self): return Capabilities()
    def play(self): return True
    def pause(self): return True
    def stop(self): return True
    def next(self): return True
    def previous(self): return True
    def select(self, index): return True


def test_manager_hears_the_active_backend_only():
    mgr = PlaybackManager()
    live = _Stub(mgr._on_backend_status)
    stale = _Stub(mgr._on_backend_status)
    mgr._active = live

    # The output the user just left fails its switch-away stop: its error
    # tick must not reach the status the new output owns.
    stale._emit(PlaybackStatus(state="stopped",
                               extra={"error": "'stale' stop failed"}))
    assert mgr.latest_status == {"state": "disconnected"}

    live._emit(PlaybackStatus(state="stopped", queue_index=0))
    assert mgr.latest_status["state"] == "stopped"
    assert "error" not in mgr.latest_status
    assert mgr.latest_status["output"]["label"] == "stub"


def test_renderer_online_is_decided_by_the_first_address_that_answers():
    with socket.socket() as srv:
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        port = srv.getsockname()[1]
        renderer = {
            # A stale address that swallows the connect (a device that left
            # this network answers nothing) must not hold up the live one.
            "location": "http://10.255.255.1:5500/desc.xml",
            "locations": [f"http://127.0.0.1:{port}/desc.xml"],
        }
        t0 = time.monotonic()
        assert renderer_online(renderer, timeout=2.0)
        assert time.monotonic() - t0 < 1.5
    assert not renderer_online(
        {"location": f"http://127.0.0.1:{port}/desc.xml"}, timeout=0.5)
    assert not renderer_online({}, timeout=0.5)


def test_a_disk_failure_is_not_the_renderer_leaving():
    backend = types.SimpleNamespace(label="KANN_ULTRA", _gone=False, _error=None,
                                    _emit_now=lambda state=None: None)
    # The library's drive unmounted under the node: the load cannot read the
    # file. The renderer answered nothing wrong — detaching it and telling
    # the user to wake it sent them after a device that was on.
    for e in (FileNotFoundError(errno.ENOENT, "No such file or directory",
                                "/music/A/B/01.flac"),
              OSError(errno.ENODEV, "No such device")):
        DlnaBackend._cmd_failed(backend, e, "load and play")
        assert backend._gone is False
        assert "did not respond" not in backend._error
    for e in (UpnpConnectionError("refused"), UpnpConnectionTimeoutError("timed out"),
              TimeoutError()):
        backend._gone = False
        DlnaBackend._cmd_failed(backend, e, "load and play")
        assert backend._gone is True


def test_a_library_gone_from_disk_is_walked_past_without_recursing(monkeypatch):
    monkeypatch.setattr(streaming_service, "_proxy",
                        MediaProxy(port=0, advertised_host="127.0.0.1"))
    monkeypatch.setattr(DlnaBackend, "_quality_suffix", staticmethod(lambda: ""))
    queue = CanonicalQueue()
    # Every owned slot of an unmounted library is one to skip, and a queue
    # longer than the old per-slot recursion could go (~1000) must end in
    # the reason, not a RecursionError.
    queue.replace([QueueItem(track_id=f"t{i}", media_file_id=i,
                             source={"kind": "file", "path": f"/nonexistent/{i}.flac",
                                     "format": "FLAC"},
                             title=f"T{i}", artist="A")
                   for i in range(1, 1201)])
    statuses = []
    backend = DlnaBackend(emit=lambda b, s: statuses.append(s), queue=queue,
                          renderer={"udn": "uuid:stub", "name": "KANN_ULTRA",
                                    "location": "http://127.0.0.1:9/desc.xml"})
    backend._dmr = types.SimpleNamespace(is_subscribed=True, transport_state=None,
                                         volume_level=None)
    assert asyncio.run(backend._load_seq(1)) is False
    assert backend._error.startswith("1200 queued track(s) could not be read")
    assert backend._gone is False
    assert statuses[-1].state == "stopped"
