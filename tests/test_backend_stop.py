"""The launcher's stop of its backend (desktop/service_manager.py,
_stop_process) and of its own sleep hold (desktop/utils.py, keep_awake).

The backend is given its own stop's time; what it leaves running — a decode,
an agent's call, its work with no one left to read it — is stopped after it,
no longer held in one wait with it until the kill. Every process stopped is
waited for: a child never waited for is left a zombie. Real processes where a
platform's own behaviour is the point (POSIX signals); a faked table for what
a real one cannot be made to do (Windows' terminate, a process ending between
two calls).
"""

import subprocess
import sys
import textwrap
import threading
import time
import types

import psutil
import pytest

from desktop import service_manager, utils
from desktop.service_manager import ServiceManager

posix = pytest.mark.skipif(sys.platform == "win32", reason="POSIX signals")

# A backend as the launcher sees one: it starts a child of its own, says the
# child's pid, and takes SIGINT as it is told to
BACKEND = textwrap.dedent("""
    import signal, subprocess, sys, time
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    if sys.argv[1] == "stops":
        signal.signal(signal.SIGINT, lambda *a: sys.exit(0))
    else:
        signal.signal(signal.SIGINT, signal.SIG_IGN)
    print(child.pid, flush=True)
    while True:
        time.sleep(1)
""")


def _backend(how: str):
    proc = subprocess.Popen([sys.executable, "-c", BACKEND, how], stdout=subprocess.PIPE, text=True)
    return proc, int(proc.stdout.readline())


def _gone(pid: int, within: float = 5) -> bool:
    deadline = time.monotonic() + within
    while psutil.pid_exists(pid):
        if time.monotonic() > deadline:
            return False
        time.sleep(0.01)
    return True


@posix
def test_a_backend_that_stops_in_time_has_what_it_left_running_stopped(caplog):
    proc, child = _backend("stops")
    try:
        began = time.monotonic()
        ServiceManager({})._stop_process(proc, "Backend")
        took = time.monotonic() - began
        # reaped by the stop itself: nobody else waits on it here
        assert not psutil.pid_exists(proc.pid)
        assert _gone(child, within=0)
        assert took < service_manager._LEFT_SECONDS + 1, took
        assert any(f"Backend left 1 process(es) running, stopping them: psutil.Process(pid={child}"
                   in r.getMessage() for r in caplog.records)
    finally:
        for pid in (proc.pid, child):
            if psutil.pid_exists(pid):
                psutil.Process(pid).kill()


@posix
def test_a_backend_that_ignores_its_stop_is_killed_and_reaped(monkeypatch, caplog):
    monkeypatch.setattr(service_manager, "_STOP_SECONDS", 0.5)
    proc, child = _backend("ignores")
    try:
        ServiceManager({})._stop_process(proc, "Backend")
        assert not psutil.pid_exists(proc.pid)
        assert _gone(child, within=0)
        said = [r.getMessage() for r in caplog.records]
        assert "Backend didn't stop in 0.5 s, killing it..." in said
        assert not [r for r in caplog.records if r.levelname == "ERROR"]
    finally:
        for pid in (proc.pid, child):
            if psutil.pid_exists(pid):
                psutil.Process(pid).kill()


class FakeProc:
    """A row of a faked process table. `gone` is what terminate() raises: a
    process that ended just before its turn."""

    def __init__(self, pid, gone=False):
        self.pid = pid
        self.ended = False
        self._gone = gone
        self.calls = []

    def terminate(self):
        self.calls.append("terminate")
        if self._gone:
            self.ended = True
            raise psutil.NoSuchProcess(self.pid)
        self.ended = True

    def kill(self):
        self.calls.append("kill")
        self.ended = True

    def send_signal(self, sig):
        self.calls.append(("signal", sig))

    def is_running(self):
        return not self.ended

    def children(self, recursive=False):
        return self._children

    def __str__(self):
        return f"FakeProc({self.pid})"


@pytest.fixture
def table(monkeypatch):
    parent = FakeProc(100)
    monkeypatch.setattr(service_manager.psutil, "Process", lambda pid: parent)
    monkeypatch.setattr(service_manager.psutil, "wait_procs",
                        lambda procs, timeout=None: ([p for p in procs if p.ended],
                                                     [p for p in procs if not p.ended]))
    return parent


def _proc(pid=100):
    return types.SimpleNamespace(pid=pid, poll=lambda: None)


def test_on_windows_the_whole_tree_is_terminated_past_a_child_gone_before_its_turn(table, monkeypatch):
    monkeypatch.setattr(service_manager, "sys", types.SimpleNamespace(platform="win32"))
    gone, worker = FakeProc(101, gone=True), FakeProc(102)
    table._children = [gone, worker]
    ServiceManager({})._stop_process(_proc(), "Backend")
    assert (gone.calls, worker.calls, table.calls) == (["terminate"], ["terminate"], ["terminate"])


def test_what_the_backend_left_is_stopped_past_one_gone_before_its_turn(table, monkeypatch):
    monkeypatch.setattr(service_manager, "sys", types.SimpleNamespace(platform="linux"))
    gone, worker = FakeProc(101, gone=True), FakeProc(102)
    table._children = [gone, worker]
    table.send_signal = lambda sig: (table.calls.append(("signal", sig)), setattr(table, "ended", True))
    ServiceManager({})._stop_process(_proc(), "Backend")
    assert (gone.calls, worker.calls) == (["terminate"], ["terminate"])


@posix
def test_the_macos_sleep_hold_is_waited_for_once_released(monkeypatch):
    """caffeinate, terminated when the services stop, was never waited for:
    a zombie of the launcher's until its next subprocess reaped it. A
    long-lived stand-in plays it."""
    held = []
    real_popen = subprocess.Popen

    def caffeinate(cmd, **kw):
        assert cmd[0] == "caffeinate"
        held.append(real_popen([sys.executable, "-c", "import time; time.sleep(60)"]))
        return held[-1]
    monkeypatch.setattr(utils, "sys", types.SimpleNamespace(platform="darwin"))
    monkeypatch.setattr(utils, "subprocess", types.SimpleNamespace(Popen=caffeinate))
    utils.keep_awake(True)
    hold = next(t for t in threading.enumerate() if t.name == "stay-awake")
    try:
        utils.keep_awake(False)
        hold.join(5)
        assert not hold.is_alive()
        assert held[0].returncode is not None
        assert not psutil.pid_exists(held[0].pid)
    finally:
        if held[0].poll() is None:
            held[0].kill()
            held[0].wait()
