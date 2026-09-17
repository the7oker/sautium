"""Playback status plumbing: the manager hears the ACTIVE backend only, and
the DLNA liveness probe is decided by the first address that answers."""

import socket
import sys
import time
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from playback.base import Capabilities, PlaybackStatus, PlayerBackend  # noqa: E402
from playback.dlna_backend import renderer_online  # noqa: E402
from playback.manager import PlaybackManager  # noqa: E402


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
