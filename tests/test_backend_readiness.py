"""The backend's readiness is two events (desktop/service_manager.py,
_await_ready): its NOTIFY carrying this start's token, or its process
ending — no /health poll. A socket that carries notify payloads stands in
for the LISTEN connection; the processes are real."""

import socket
import subprocess
import sys
import threading
import types

import psycopg2
import pytest

from desktop import service_manager
from desktop.service_manager import ServiceManager


class FakeListener:
    """select()-able like a psycopg2 connection; poll() moves what arrived
    into notifies — newline-separated payloads written to the other end."""

    def __init__(self):
        self.sock, self.peer = socket.socketpair()
        self.notifies = []
        self.broken = False
        self.closed = False

    def fileno(self):
        return self.sock.fileno()

    def notify(self, payload: str):
        self.peer.send(payload.encode() + b"\n")

    def poll(self):
        if self.broken:
            raise psycopg2.OperationalError("server closed the connection unexpectedly")
        data = self.sock.recv(4096).decode()
        self.notifies += [types.SimpleNamespace(payload=p) for p in data.split("\n") if p]

    def close(self):
        self.closed = True
        self.sock.close()
        self.peer.close()


@pytest.fixture
def wake():
    r, w = socket.socketpair()
    yield r, w
    r.close()
    w.close()


def test_ready_is_this_starts_token(wake):
    listener = FakeListener()
    listener.notify("an-earlier-start")
    listener.notify("tok")
    assert ServiceManager._next_verdict(listener, wake[0], "tok", 5) == "ready"


def test_another_starts_token_waits_on_to_the_deadline(wake):
    listener = FakeListener()
    listener.notify("an-earlier-start")
    assert ServiceManager._next_verdict(listener, wake[0], "tok", 0.3) is None


def test_a_broken_listen_connection_is_lost(wake):
    listener = FakeListener()
    listener.broken = True
    listener.notify("tok")
    assert ServiceManager._next_verdict(listener, wake[0], "tok", 5) == "lost"


def test_the_process_ending_wakes_the_wait():
    listener = FakeListener()
    wake_r, wake_w = socket.socketpair()
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    threading.Thread(target=ServiceManager._signal_exit, args=(proc, wake_w),
                     daemon=True).start()
    assert ServiceManager._next_verdict(listener, wake_r, "tok", 10) == "exited"
    wake_r.close()


def test_a_backend_that_answers_after_its_window_is_reported_up(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.setattr(service_manager, "_READY_PATIENCE_SECONDS", 0.3)
    came_up = threading.Event()
    monkeypatch.setattr(ServiceManager, "_backend_up",
                        lambda self, proc, port: came_up.set())
    sm = ServiceManager({"ports": {"web": 18999}})
    listener = FakeListener()
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    sm.backend_proc = proc
    try:
        failure = sm._await_ready(proc, listener, "tok", 18999)
        assert failure and "No answer" in failure      # the caller hears "down"…
        assert not came_up.is_set()
        listener.notify("tok")                          # …and the late answer still lands
        assert came_up.wait(10)
        assert listener.closed
    finally:
        proc.kill()
        proc.wait()


def test_a_backend_that_dies_after_its_window_is_not_reported_up(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.setattr(service_manager, "_READY_PATIENCE_SECONDS", 0.3)
    came_up = threading.Event()
    monkeypatch.setattr(ServiceManager, "_backend_up",
                        lambda self, proc, port: came_up.set())
    sm = ServiceManager({"ports": {"web": 18999}})
    listener = FakeListener()
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    sm.backend_proc = proc
    before = set(threading.enumerate())
    assert sm._await_ready(proc, listener, "tok", 18999)
    late = next(t for t in set(threading.enumerate()) - before if t.name == "backend-late-ready")
    proc.kill()
    late.join(10)
    assert not late.is_alive() and listener.closed
    assert not came_up.is_set()
