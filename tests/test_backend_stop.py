"""The launcher's stop of its backend (desktop/service_manager.py,
_stop_process) and of its own sleep hold (desktop/utils.py, keep_awake).

The backend is given its own stop's time; what it leaves running — a decode,
an agent's call, its work with no one left to read it — is stopped after it,
no longer held in one wait with it until the kill. Every process stopped is
waited for: a child never waited for is left a zombie. Real processes where a
platform's own behaviour is the point (POSIX signals); they take it that an
orphan is reaped by whoever reaps here — init, launchd, the shell a test
container runs as its PID 1. A faked table for what a real one cannot be made
to do (Windows' terminate, a process ending between two calls, one that
outlives its kill).
"""

import errno
import logging
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

# A backend as the launcher sees one: it starts a child of its own — one that
# ignores SIGTERM when told to be stubborn — says the child's pid, and takes
# SIGINT as it is told to
BACKEND = textwrap.dedent("""
    import signal, subprocess, sys, time
    child = subprocess.Popen([sys.executable, "-c",
        "import signal, sys, time\\n"
        "if sys.argv[1] == 'stubborn': signal.signal(signal.SIGTERM, signal.SIG_IGN)\\n"
        "print('ready', flush=True); time.sleep(60)", sys.argv[2]], stdout=subprocess.PIPE, text=True)
    assert child.stdout.readline().strip() == "ready"
    if sys.argv[1] == "stops":
        signal.signal(signal.SIGINT, lambda *a: sys.exit(0))
    else:
        signal.signal(signal.SIGINT, signal.SIG_IGN)
    print(child.pid, flush=True)
    while True:
        time.sleep(1)
""")


def _backend(how: str, child: str = "plain"):
    proc = subprocess.Popen([sys.executable, "-c", BACKEND, how, child], stdout=subprocess.PIPE, text=True)
    return proc, int(proc.stdout.readline())


def _gone(pid: int, within: float = 5) -> bool:
    deadline = time.monotonic() + within
    while psutil.pid_exists(pid):
        if time.monotonic() > deadline:
            return False
        time.sleep(0.01)
    return True


def _cleanup(*pids):
    for pid in pids:
        if psutil.pid_exists(pid):
            psutil.Process(pid).kill()


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
        assert any(f"Backend left 1 process(es) running, stopping them: {child} ("
                   in r.getMessage() for r in caplog.records)
    finally:
        _cleanup(proc.pid, child)


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
        _cleanup(proc.pid, child)


@posix
def test_what_the_backend_left_that_ignores_its_terminate_is_killed_and_reaped(monkeypatch, caplog):
    monkeypatch.setattr(service_manager, "_LEFT_SECONDS", 0.3)
    proc, child = _backend("stops", child="stubborn")
    try:
        ServiceManager({})._stop_process(proc, "Backend")
        assert _gone(child, within=0)
        assert not [r for r in caplog.records if r.levelname == "ERROR"]
    finally:
        _cleanup(proc.pid, child)


class FakeProc:
    """A row of a faked process table, its calls kept in `log` in the order
    they came. `gone` is what terminate() raises: a process that ended just
    before its turn; `stays` outlives even its kill; `names` is what
    reading its name raises."""

    def __init__(self, pid, log, gone=False, stays=False, names=None):
        self.pid = pid
        self.ended = False
        self.log = log
        self._gone, self._stays, self._names = gone, stays, names
        self._children = []

    def terminate(self):
        self.log.append((self.pid, "terminate"))
        if self._gone:
            self.ended = True
            raise psutil.NoSuchProcess(self.pid)
        self.ended = not self._stays

    def kill(self):
        self.log.append((self.pid, "kill"))
        self.ended = not self._stays

    def send_signal(self, sig):
        self.log.append((self.pid, "signal"))
        self.ended = True

    def is_running(self):
        return not self.ended

    def name(self):
        if self._names is not None:
            raise self._names
        return "worker"

    def children(self, recursive=False):
        if isinstance(self._children, BaseException):
            raise self._children
        return self._children


@pytest.fixture
def table(monkeypatch):
    """The backend's process (pid 100), its calls in table.log."""
    log = []
    parent = FakeProc(100, log)
    monkeypatch.setattr(service_manager.psutil, "Process", lambda pid: parent)
    monkeypatch.setattr(service_manager.psutil, "wait_procs",
                        lambda procs, timeout=None: ([p for p in procs if p.ended],
                                                     [p for p in procs if not p.ended]))
    return parent


def _proc(pid=100):
    return types.SimpleNamespace(pid=pid, poll=lambda: None)


def _platform(monkeypatch, name):
    monkeypatch.setattr(service_manager, "sys", types.SimpleNamespace(platform=name))


def test_on_windows_the_parent_is_terminated_first_then_its_tree_past_a_gone_child(table, monkeypatch):
    _platform(monkeypatch, "win32")
    table._children = [FakeProc(101, table.log, gone=True), FakeProc(102, table.log)]
    ServiceManager({})._stop_process(_proc(), "Backend")
    assert table.log == [(100, "terminate"), (101, "terminate"), (102, "terminate")]


def test_what_the_backend_left_is_stopped_past_one_gone_before_its_turn(table, monkeypatch):
    _platform(monkeypatch, "linux")
    table._children = [FakeProc(101, table.log, gone=True), FakeProc(102, table.log)]
    ServiceManager({})._stop_process(_proc(), "Backend")
    assert table.log == [(100, "signal"), (101, "terminate"), (102, "terminate")]


@pytest.mark.parametrize("raised", [psutil.AccessDenied(100), PermissionError(errno.EACCES, "/proc/1/stat")],
                         ids=["hidepid", "hidepid-psutil-6"])
def test_a_backend_whose_processes_cannot_be_listed_is_stopped_all_the_same(table, monkeypatch, caplog,
                                                                             raised):
    _platform(monkeypatch, "linux")
    table._children = raised
    with caplog.at_level(logging.WARNING):
        ServiceManager({})._stop_process(_proc(), "Backend")
    assert table.log == [(100, "signal")]
    assert [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING] == [
        f"Backend's processes could not be listed: {raised!r}"]


def test_one_that_outlives_its_kill_is_said(table, monkeypatch, caplog):
    _platform(monkeypatch, "linux")
    table._children = [FakeProc(101, table.log, stays=True)]
    with caplog.at_level(logging.WARNING):
        ServiceManager({})._stop_process(_proc(), "Backend")
    assert table.log == [(100, "signal"), (101, "terminate"), (101, "kill")]
    assert [r.getMessage() for r in caplog.records if r.levelno == logging.ERROR] == [
        "Backend: still running after the kill: 101 (worker)"]


@pytest.mark.parametrize("raised", [psutil.NoSuchProcess(101), RuntimeError("proc_pidpath() syscall failed"),
                                    OSError(errno.EIO, "sysctl"),
                                    SystemError("returned a result with an exception set")],
                         ids=["vanished", "runtime-error", "os-error", "system-error"])
def test_what_it_left_is_stopped_whatever_reading_its_name_raises(table, monkeypatch, caplog, raised):
    _platform(monkeypatch, "linux")
    table._children = [FakeProc(101, table.log, names=raised), FakeProc(102, table.log)]
    with caplog.at_level(logging.WARNING):
        ServiceManager({})._stop_process(_proc(), "Backend")
    assert table.log == [(100, "signal"), (101, "terminate"), (102, "terminate")]
    assert "Backend left 2 process(es) running, stopping them: 101, 102 (worker)" in [
        r.getMessage() for r in caplog.records]


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
