"""The launcher's orphan sweep (desktop/service_manager.py,
_kill_orphan_on_port) never fails a start.

Before every backend start the launcher stops whatever a previous session
left listening on the web port. On 2026-10-25 a Mac's restart failed at that
sweep: psutil 7.2.2 on macOS 27 raised RuntimeError for Xcode's DTServiceHub,
a live process of the user's whose descriptors the kernel would not list —
on every read, so every start failed while it ran. A zombie there raised
ZombieProcess, and a process exiting under the read NoSuchProcess, then
ZombieProcess (both seen on that Mac the same day). Whatever psutil raises
for one process, that process is not the orphan; what the sweep cannot
settle, the backend's own bind says.

The process table is faked for what a real one cannot be made to do; one
real orphan is found by its socket and stopped.
"""

import errno
import logging
import os
import socket
import subprocess
import sys
import types

import psutil
import pytest

from desktop import service_manager
from desktop.service_manager import ServiceManager

PORT = 18999


def _conn(port: int, status: str) -> types.SimpleNamespace:
    return types.SimpleNamespace(status=status, laddr=types.SimpleNamespace(ip="0.0.0.0", port=port))


class FakeProc:
    """One row of the table: its sockets, or what reading them raises, and
    the stops it is sent. `waits` is what each wait() raises in turn (None:
    it returns)."""

    def __init__(self, pid, conns=(), reads=None, name="Python", names=None,
                 stops=None, waits=()):
        self.pid = pid
        self._conns = list(conns)
        self._reads = reads
        self._name, self._names = name, names
        self._stops = stops
        self._waits = list(waits)
        self.calls = []

    def net_connections(self, kind):
        assert kind == "tcp"
        if self._reads is not None:
            raise self._reads
        return self._conns

    def name(self):
        if self._names is not None:
            raise self._names
        return self._name

    def terminate(self):
        self.calls.append("terminate")
        if self._stops is not None:
            raise self._stops

    def kill(self):
        self.calls.append("kill")

    def wait(self, timeout=None):
        self.calls.append(("wait", timeout))
        if self._waits:
            raised = self._waits.pop(0)
            if raised is not None:
                raise raised


@pytest.fixture
def table(monkeypatch):
    """The process table the sweep reads: rows by pid, in order. A row that
    is an exception is a pid gone before its Process could be made."""
    rows = {}

    def process(pid):
        row = rows[pid]
        if isinstance(row, BaseException):
            raise row
        return row
    monkeypatch.setattr(service_manager.psutil, "pids", lambda: list(rows))
    monkeypatch.setattr(service_manager.psutil, "Process", process)
    return rows


def _orphan(pid=300, **kw):
    return FakeProc(pid, conns=[_conn(PORT, psutil.CONN_LISTEN)], **kw)


def _warnings(caplog):
    return [r.getMessage() for r in caplog.records
            if r.name == "desktop.service_manager" and r.levelno == logging.WARNING]


def test_the_orphan_listening_on_the_port_is_stopped_and_waited_for(table, caplog):
    client = FakeProc(100, conns=[_conn(PORT, psutil.CONN_ESTABLISHED)])   # a browser's socket to it
    other = FakeProc(200, conns=[_conn(PORT + 1, psutil.CONN_LISTEN)])
    orphan = _orphan()
    table.update({100: client, 200: other, 300: orphan})
    with caplog.at_level(logging.WARNING):
        ServiceManager._kill_orphan_on_port(PORT)
    assert orphan.calls == ["terminate", ("wait", 10)]
    assert client.calls == other.calls == []
    assert _warnings(caplog) == [f"Killing orphan process on port {PORT}: PID 300 (Python)"]


@pytest.mark.parametrize("raised", [
    # a zombie, as psutil 7.2.2 says for one on macOS 27 — it holds no socket
    psutil.ZombieProcess(150, "sleep", 1),
    # one on Linux: its /proc/<pid>/fd is root's once it has no memory
    psutil.AccessDenied(150, "sleep"),
    # one that ended under the read
    psutil.NoSuchProcess(150),
    # another user's
    psutil.AccessDenied(150, "login"),
    # a live one of the user's whose descriptors the kernel will not list
    RuntimeError("proc_pidinfo(PROC_PIDLISTFDS) 2/2 syscall failed"),
    # an errno psutil passes on as it is
    OSError(errno.EINVAL, "Invalid argument"),
], ids=["zombie-macos", "zombie-linux", "vanished", "another-users", "runtime-error", "os-error"])
def test_a_process_whose_sockets_cannot_be_read_is_not_the_orphan(table, raised):
    orphan = _orphan()
    table.update({150: FakeProc(150, reads=raised), 300: orphan})
    ServiceManager._kill_orphan_on_port(PORT)
    assert orphan.calls == ["terminate", ("wait", 10)]


@pytest.mark.parametrize("raised", [
    psutil.NoSuchProcess(150),
    OSError(errno.EIO, "Input/output error"),
], ids=["vanished", "os-error"])
def test_a_process_that_cannot_be_made_is_not_the_orphan(table, raised):
    """Gone between the table and its Process, or unreadable from the
    start (macOS's kinfo read passes on an errno)."""
    orphan = _orphan()
    table.update({150: raised, 300: orphan})
    ServiceManager._kill_orphan_on_port(PORT)
    assert orphan.calls == ["terminate", ("wait", 10)]


def test_a_sweep_that_reads_no_process_says_so_once(table, caplog):
    """pid 0 — macOS answers its read with no syscall at all — and the
    launcher itself are read on every system: neither can be the orphan,
    and a sweep that read only them read nothing."""
    table.update({pid: FakeProc(pid, reads=RuntimeError("proc_pidinfo(PROC_PIDLISTFDS) 2/2 syscall failed"))
                  for pid in (1, 2, 3)})
    table.update({0: FakeProc(0), os.getpid(): FakeProc(os.getpid())})
    with caplog.at_level(logging.WARNING):
        ServiceManager._kill_orphan_on_port(PORT)
    assert _warnings(caplog) == [f"Orphan sweep: no process's sockets could be read — "
                                 f"nothing on port {PORT} was checked"]


@pytest.mark.parametrize("raised", [RuntimeError("no PIDs found"), OSError(errno.ENOMEM, "Cannot allocate memory")],
                         ids=["runtime-error", "os-error"])
def test_a_process_table_that_cannot_be_read_says_so_and_the_start_goes_on(monkeypatch, caplog, raised):
    def pids():
        raise raised
    monkeypatch.setattr(service_manager.psutil, "pids", pids)
    with caplog.at_level(logging.WARNING):
        ServiceManager._kill_orphan_on_port(PORT)
    assert _warnings(caplog) == [f"Orphan sweep: the process table could not be read ({raised!r}) — "
                                 f"nothing on port {PORT} was checked"]


def test_an_orphan_that_outlives_its_terminate_is_killed_and_waited_for(table):
    orphan = _orphan(waits=[psutil.TimeoutExpired(5, 300)])
    table[300] = orphan
    ServiceManager._kill_orphan_on_port(PORT)
    assert orphan.calls == ["terminate", ("wait", 10), "kill", ("wait", 5)]


@pytest.mark.parametrize("orphan, calls, said", [
    # Windows lists every user's sockets: a service's terminate is refused
    (_orphan(stops=psutil.AccessDenied(300, "svchost.exe")), ["terminate"],
     f"Orphan PID 300 on port {PORT} not stopped: "),
    # one that outlives its kill too
    (_orphan(waits=[psutil.TimeoutExpired(5, 300), psutil.TimeoutExpired(5, 300)]),
     ["terminate", ("wait", 10), "kill", ("wait", 5)],
     f"Orphan PID 300 on port {PORT} not stopped: "),
    # an errno psutil passes on as it is: Windows' TerminateProcess, its wait
    (_orphan(stops=OSError(errno.EIO, "TerminateProcess")), ["terminate"],
     f"Orphan PID 300 on port {PORT} not stopped: "),
    (_orphan(waits=[OSError(errno.EIO, "WaitForSingleObject")]), ["terminate", ("wait", 10)],
     f"Orphan PID 300 on port {PORT} not stopped: "),
    # one that ended by itself between the read and the stop: nothing to say
    (_orphan(stops=psutil.NoSuchProcess(300)), ["terminate"], None),
], ids=["refused", "unkillable", "terminate-os-error", "wait-os-error", "ended"])
def test_an_orphan_that_cannot_be_stopped_leaves_the_start_to_the_bind(table, caplog, orphan, calls,
                                                                         said):
    second = _orphan(pid=400)       # on another address of the same port
    table.update({300: orphan, 400: second})
    with caplog.at_level(logging.WARNING):
        ServiceManager._kill_orphan_on_port(PORT)
    assert orphan.calls == calls
    assert second.calls == ["terminate", ("wait", 10)]
    not_stopped = [line for line in _warnings(caplog) if " not stopped: " in line]
    assert [line.startswith(said) for line in not_stopped] == ([True] if said else [])


@pytest.mark.parametrize("raised", [
    psutil.NoSuchProcess(300),                       # it ended after its sockets were read
    psutil.AccessDenied(300),
    OSError(errno.EIO, "Input/output error"),
    RuntimeError("proc_pidpath() syscall failed"),
    SystemError("returned a result with an exception set"),   # macOS, a long name's cmdline
], ids=["vanished", "denied", "os-error", "runtime-error", "system-error"])
def test_an_orphan_whose_name_cannot_be_read_is_stopped_all_the_same(table, caplog, raised):
    orphan = _orphan(names=raised)
    table[300] = orphan
    with caplog.at_level(logging.WARNING):
        ServiceManager._kill_orphan_on_port(PORT)
    assert orphan.calls == ["terminate", ("wait", 10)]
    assert _warnings(caplog) == [f"Killing orphan process on port {PORT}: PID 300"]


def test_a_real_orphan_is_found_by_its_socket_stopped_and_reaped():
    """psutil's own table on this machine — on a Mac with Xcode running, the
    process the sweep failed on is in it."""
    orphan = subprocess.Popen(
        [sys.executable, "-c", "import socket, sys, time\n"
         "s = socket.socket(); s.bind(('127.0.0.1', 0)); s.listen()\n"
         "print(s.getsockname()[1], flush=True); time.sleep(60)"],
        stdout=subprocess.PIPE, text=True)
    try:
        port = int(orphan.stdout.readline())
        ServiceManager._kill_orphan_on_port(port)
        # waited for by the sweep itself: reaped, no zombie left of it
        assert orphan.poll() is not None
        assert not psutil.pid_exists(orphan.pid)
        with socket.socket() as s:
            s.bind(("127.0.0.1", port))
    finally:
        if orphan.poll() is None:
            orphan.kill()
            orphan.wait()
