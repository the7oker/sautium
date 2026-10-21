"""A word before dying, from the launcher and from the backend.

The launcher (desktop/utils.watch_signals): a signal from outside is said in
its log and quits as Quit does; the same interrupt or termination again ends
the process at once — a hangup never, a closed terminal sends two — and
another one while it quits, a stop's companion, changes nothing. Its service
manager takes a backend that ends once the quit began for no crash. The
backend's uvicorn server (backend/serving.py), as the Docker entrypoint and
the launcher's backend_serve run it: it says the signal that stops it, a
hangup stops it as SIGTERM does, a signal while it stops does not cut the
lifespan's shutdown short, and before it serves a stop ends the process at
once.

Real processes and real signals, so POSIX only: on Windows the launcher's
words come from the console's events, which no test here sends. The
backend's own main()s run with a fake main.py, and their PostgreSQL
is the test cluster's.
"""

import os
import queue
import re
import signal
import socket
import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX signals")

LAUNCHER = textwrap.dedent("""
    import logging, sys, time
    sys.path.insert(0, sys.argv[1])
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s", stream=sys.stdout)
    from desktop import utils
    asked = []
    # As the launcher's ui_call: the quit runs on the main loop, once the handler returned
    utils.watch_signals(lambda: asked.append("quit"), lambda: print("stop now", flush=True))
    print("ready", flush=True)
    while True:
        time.sleep(0.05)
        while asked:
            print(asked.pop(0), flush=True)
""")

# The quit raises SIGINT again: its handler runs inside the first one's
NESTED = LAUNCHER.replace('lambda: asked.append("quit")',
                          'lambda: (asked.append("quit"), signal.raise_signal(signal.SIGINT))'
                          ).replace("import logging, sys, time", "import logging, signal, sys, time")

# The backend's app as these tests fake it: a stream that never ends by
# itself, as the Web UI's events do, and a lifespan that says its shutdown
APP = textwrap.dedent("""
    import asyncio

    async def app(scope, receive, send):
        if scope["type"] == "http":
            await send({"type": "http.response.start", "status": 200, "headers": []})
            while True:
                await send({"type": "http.response.body", "body": b"tick", "more_body": True})
                await asyncio.sleep(0.05)
        while True:
            message = await receive()
            if message["type"] == "lifespan.startup":
                await send({"type": "lifespan.startup.complete"})
            elif message["type"] == "lifespan.shutdown":
                print("lifespan shutdown", flush=True)
                await send({"type": "lifespan.shutdown.complete"})
                return
""")

BACKEND = APP + textwrap.dedent("""
    import logging, sys
    sys.path.insert(0, sys.argv[1])
    logging.basicConfig(level=logging.INFO, format="%(name)s %(levelname)s %(message)s", stream=sys.stdout)
    import uvicorn
    from serving import Server, served

    class Ready(Server):
        async def startup(self, sockets=None):
            await super().startup(sockets=sockets)
            print("ready", self.servers[0].sockets[0].getsockname()[1], flush=True)

    served()
    server = Ready(uvicorn.Config(app, host="127.0.0.1", port=0, log_config=None, lifespan="on",
                                  timeout_graceful_shutdown=1))
    try:
        server.run()
    except KeyboardInterrupt:
        print("interrupted as it left", flush=True)
    print("left", flush=True)
""")

BEFORE = textwrap.dedent("""
    import logging, sys, time
    sys.path.insert(0, sys.argv[1])
    logging.basicConfig(level=logging.INFO, format="%(name)s %(levelname)s %(message)s", stream=sys.stdout)
    from serving import heard_until_served
    heard_until_served()
    print("ready", flush=True)
    while True:             # the entrypoint's wait for the database
        time.sleep(0.05)
""")

# The two ways a node runs the backend, as they are: "main:app" is the fake
ENTRYPOINT = "import sys, entrypoint; sys.exit(entrypoint.main())"
SERVE = "from desktop import backend_serve; backend_serve.main()"
# The app's logging (config.LOGGING_CONFIG), once uvicorn listens
LISTENING = " - uvicorn.error - INFO - Uvicorn running on "

WAIT_S = 10


class Proc:
    """A process the test signals, its lines read on a thread of their own:
    the next one, or a failed test, never a hang. It is ready once a line
    holds `ready`."""

    def __init__(self, script: str, arg, ready: str = "ready", **popen):
        self.p = subprocess.Popen([sys.executable, "-c", script, str(arg)], stdout=subprocess.PIPE,
                                  stderr=subprocess.STDOUT, text=True, **popen)
        self._lines: queue.Queue = queue.Queue()
        threading.Thread(target=self._read, daemon=True).start()
        self.ready = self.line()
        while ready not in self.ready:
            self.ready = self.line()

    def _read(self) -> None:
        for line in self.p.stdout:
            self._lines.put(line.rstrip("\n"))
        self._lines.put(None)

    def line(self) -> str:
        line = self._lines.get(timeout=WAIT_S)
        assert line is not None, f"the process ended (exit {self.p.wait()})"
        return line

    def until(self, head: str) -> str:
        """Its next line that starts with `head`, past any other."""
        line = self.line()
        while not line.startswith(head):
            line = self.line()
        return line

    def rest(self) -> list:
        """Everything it says until it ends, and how it ended."""
        lines = []
        while (line := self._lines.get(timeout=WAIT_S)) is not None:
            lines.append(line)
        return lines + [f"exit {self.p.wait(WAIT_S)}"]

    def send(self, sig) -> None:
        self.p.send_signal(sig)

    def close(self) -> None:
        self.p.kill()
        self.p.wait()


def _closed_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _port(listening: str) -> int:
    """The port uvicorn bound, as its "Uvicorn running on" line names it."""
    return int(re.search(r"http://[^:/]+:(\d+)", listening).group(1))


@pytest.fixture
def fake_main(tmp_path):
    """The directory a backend process starts in: its main.py the app above,
    everything else the backend's own."""
    (tmp_path / "main.py").write_text(APP)
    return tmp_path


@pytest.fixture
def database():
    """The settings a backend process reads for PostgreSQL: the test
    cluster's own database."""
    psycopg2 = pytest.importorskip("psycopg2")
    from conftest import PG
    try:
        psycopg2.connect(dbname="postgres", connect_timeout=3, **PG).close()
    except psycopg2.OperationalError:
        pytest.skip("no PostgreSQL")
    return {"POSTGRES_HOST": PG["host"], "POSTGRES_PORT": str(PG["port"]),
            "POSTGRES_USER": PG["user"], "POSTGRES_PASSWORD": PG["password"],
            "POSTGRES_DB": "postgres"}


def _env(fake_main: Path, **more) -> dict:
    return {**os.environ, "PYTHONPATH": os.pathsep.join([str(REPO), str(REPO / "backend")]),
            "PYTHONDONTWRITEBYTECODE": "1", "HF_HOME": str(fake_main / "hf"), **more}


def _said(out: list) -> list:
    return [line.split(" - serving - INFO - ", 1)[1] for line in out if " - serving - INFO - " in line]


@pytest.mark.parametrize("sig", [signal.SIGTERM, signal.SIGINT])
def test_the_launcher_says_a_signal_quits_as_quit_does_and_ends_at_the_same_again(sig):
    p = Proc(LAUNCHER, REPO)
    try:
        p.send(sig)
        assert p.line() == f"INFO {sig.name}: the launcher quits as Quit does"
        assert p.line() == "quit"
        p.send(sig)
        assert p.rest() == [f"WARNING {sig.name} again while the launcher quits: it ends now",
                            f"exit {128 + sig}"]
    finally:
        p.close()


def test_a_hangup_again_lets_the_launchers_quit_go_on():
    """A terminal that closes sends its foreground process SIGHUP twice —
    its shell's, then the kernel's as the shell exits: the second is no
    person's "end it now"."""
    p = Proc(LAUNCHER, REPO)
    try:
        p.send(signal.SIGHUP)
        assert p.line() == "INFO SIGHUP: the launcher quits as Quit does"
        assert p.line() == "quit"
        p.send(signal.SIGHUP)
        assert p.line() == "INFO SIGHUP while the launcher quits: it goes on quitting"
        assert p.p.poll() is None
    finally:
        p.close()


def test_a_signal_the_launcher_was_started_ignoring_stays_ignored():
    """nohup's hangup, a shell's background job's interrupt: they stay
    ignored, as CPython keeps an ignored SIGINT — a launcher started under
    nohup outlives its terminal as before. A termination still quits."""
    def ignoring():
        signal.signal(signal.SIGHUP, signal.SIG_IGN)
        signal.signal(signal.SIGINT, signal.SIG_IGN)
    p = Proc(LAUNCHER, REPO, preexec_fn=ignoring)
    try:
        p.send(signal.SIGHUP)
        p.send(signal.SIGINT)
        p.send(signal.SIGTERM)
        assert p.line() == "INFO SIGTERM: the launcher quits as Quit does"
    finally:
        p.close()


def test_a_signal_heard_inside_the_first_ones_handler_is_no_repeat():
    """CPython runs a handler between any two bytecodes, a handler's own:
    one that lands inside the first, before its quit began (two of a closed
    terminal's hangups did, measured), quits nothing twice and ends nothing
    at once. The next one is a repeat."""
    p = Proc(NESTED, REPO)
    try:
        p.send(signal.SIGINT)
        assert p.line() == "INFO SIGINT: the launcher quits as Quit does"
        assert p.line() == "INFO SIGINT while the launcher quits: it goes on quitting"
        assert p.line() == "quit"
        assert p.p.poll() is None
        p.send(signal.SIGINT)
        assert p.rest() == ["WARNING SIGINT again while the launcher quits: it ends now",
                            f"exit {128 + signal.SIGINT}"]
    finally:
        p.close()


def test_a_stops_companion_signal_lets_the_launchers_quit_go_on():
    """systemd sends SIGHUP beside its SIGTERM: the quit goes on, and only
    the same termination again ends the process at once."""
    p = Proc(LAUNCHER, REPO)
    try:
        p.send(signal.SIGTERM)
        assert p.line() == "INFO SIGTERM: the launcher quits as Quit does"
        assert p.line() == "quit"
        p.send(signal.SIGHUP)
        assert p.line() == "INFO SIGHUP while the launcher quits: it goes on quitting"
        assert p.p.poll() is None
        p.send(signal.SIGTERM)
        assert p.rest() == ["WARNING SIGTERM again while the launcher quits: it ends now",
                            f"exit {128 + signal.SIGTERM}"]
    finally:
        p.close()


def test_a_backend_that_ends_once_the_launcher_quits_is_no_crash(monkeypatch):
    """A session's end signals each of its processes: the backend's own stop
    may end it before the launcher stops it. The quit's first step,
    closing(), is on the Tk thread and never waits for the lifecycle lock a
    PostgreSQL start holds for up to a minute; after it, the watch neither
    reports the backend's end nor restarts it — as it does without it."""
    from desktop.service_manager import ServiceManager

    def watched(close: bool) -> list:
        sm = ServiceManager({})
        did = []
        monkeypatch.setattr(sm, "_notify_backend_event",
                            lambda state, message, **kw: did.append(state))
        monkeypatch.setattr(sm, "_read_backend_log_tail", lambda: "")
        monkeypatch.setattr(sm, "start_backend", lambda *a, **kw: did.append("start") or True)
        sm.backend_proc = subprocess.Popen([sys.executable, "-c", "pass"])
        if close:
            held, over = threading.Event(), threading.Event()

            def starting():                     # a PostgreSQL start under way
                with sm._lifecycle:
                    held.set()
                    over.wait(5)
            start = threading.Thread(target=starting)
            start.start()
            held.wait()
            began = time.monotonic()
            sm.closing()
            assert time.monotonic() - began < 0.5
            over.set()
            start.join()
        sm._watch_backend(sm.backend_proc)      # returns once the process ended
        return did

    assert watched(close=True) == []
    assert watched(close=False) == ["crashed", "start", "restarted"]


@pytest.mark.parametrize("sig, said, ends", [
    (signal.SIGTERM, ["serving INFO SIGTERM received: the backend shuts down"], -signal.SIGTERM),
    (signal.SIGINT, ["serving INFO SIGINT received: the backend shuts down"], 0),
    (signal.SIGHUP, ["serving INFO SIGHUP received: taken as SIGTERM",
                     "serving INFO SIGTERM received: the backend shuts down"], -signal.SIGTERM),
])
def test_the_backend_says_the_signal_that_stops_it_and_shuts_down_in_order(sig, said, ends):
    p = Proc(BACKEND, REPO / "backend")
    try:
        p.send(sig)
        out = p.rest()
    finally:
        p.close()
    assert [line for line in out if line.startswith("serving ")] == said, out
    assert "lifespan shutdown" in out, out
    assert out[-1] == f"exit {ends}", out
    # SIGINT, raised again as uvicorn leaves, is the caller's to end quietly
    assert ("left" in out) == (ends == 0) and not any("Traceback" in line for line in out), out


def test_a_signal_while_the_backend_stops_keeps_its_shutdown_whole():
    """A session's end signals each of its processes: the backend stops on
    its SIGTERM, an event stream still open, and the launcher, quitting,
    then stops it with SIGINT. uvicorn's force exit on that second signal
    skipped the lifespan's shutdown; the stop goes on as it began."""
    p = Proc(BACKEND, REPO / "backend")
    stream = socket.create_connection(("127.0.0.1", int(p.ready.split()[1])), timeout=WAIT_S)
    try:
        stream.sendall(b"GET / HTTP/1.1\r\nHost: test\r\n\r\n")
        assert stream.recv(4096).startswith(b"HTTP/1.1 200")
        p.send(signal.SIGTERM)
        assert p.until("serving ") == "serving INFO SIGTERM received: the backend shuts down"
        # In the stop's wait for the stream, which uvicorn says as it begins
        p.until("uvicorn.error INFO Waiting for connections to close")
        p.send(signal.SIGINT)
        out = p.rest()
    finally:
        stream.close()
        p.close()
    assert not [line for line in out if line.startswith("serving ")], out
    assert "lifespan shutdown" in out and out[-1] == f"exit {-signal.SIGTERM}", out


@pytest.mark.parametrize("sig", [signal.SIGTERM, signal.SIGINT, signal.SIGHUP])
def test_a_stop_before_the_server_serves_ends_the_process_at_once(sig):
    """PID 1 in a container has no default action for SIGTERM: the
    entrypoint's wait for the database ignored a stop until its SIGKILL."""
    p = Proc(BEFORE, REPO / "backend")
    try:
        p.send(sig)
        assert p.rest() == [
            f"serving INFO {sig.name} received before the server started: the backend stops",
            "exit 0"]
    finally:
        p.close()


def test_the_entrypoint_says_a_stop_during_its_wait_for_the_database(fake_main):
    """The Docker entrypoint's own main(): its logging and its stops are set
    before the wait, which a closed PostgreSQL port keeps going."""
    p = Proc(ENTRYPOINT, "", ready="[entrypoint] database unreachable", cwd=fake_main,
             env=_env(fake_main, POSTGRES_HOST="127.0.0.1", POSTGRES_PORT=str(_closed_port()),
                      UVICORN_RELOAD="false"))
    try:
        p.send(signal.SIGTERM)
        out = p.rest()
    finally:
        p.close()
    assert _said(out) == ["SIGTERM received before the server started: the backend stops"], out
    assert out[-1] == "exit 0", out


@pytest.mark.parametrize("sig, said", [
    (signal.SIGTERM, ["SIGTERM received: the backend shuts down"]),
    (signal.SIGHUP, ["SIGHUP received: taken as SIGTERM", "SIGTERM received: the backend shuts down"]),
])
def test_the_entrypoint_serves_and_says_the_signal_that_stops_it(fake_main, database, sig, said):
    """The Docker entrypoint's own main(), past the wait: its server is
    serving.Server with the app's logging, and a hangup stops it as SIGTERM
    does, the lifespan's shutdown whole with an event stream open. Not PID 1
    here: the SIGTERM raised again as the server leaves ends the process."""
    p = Proc(ENTRYPOINT, "", ready=LISTENING, cwd=fake_main,
             env=_env(fake_main, **database, UVICORN_HOST="127.0.0.1", UVICORN_PORT="0",
                      UVICORN_RELOAD="false"))
    stream = socket.create_connection(("127.0.0.1", _port(p.ready)), timeout=WAIT_S)
    try:
        stream.sendall(b"GET / HTTP/1.1\r\nHost: test\r\n\r\n")
        assert stream.recv(4096).startswith(b"HTTP/1.1 200")
        p.send(sig)
        out = p.rest()
    finally:
        stream.close()
        p.close()
    assert _said(out) == said, out
    assert "lifespan shutdown" in out and out[-1] == f"exit {-signal.SIGTERM}", out


@pytest.mark.parametrize("sig, said, ends", [
    (signal.SIGINT, ["SIGINT received: the backend shuts down"], 0),
    (signal.SIGHUP, ["SIGHUP received: taken as SIGTERM", "SIGTERM received: the backend shuts down"],
     -signal.SIGTERM),
])
def test_the_launchers_backend_says_the_signal_that_stops_it(fake_main, database, sig, said, ends):
    """The launcher's own backend_serve.main(): the launcher stops it with
    SIGINT, which ends it quietly — no traceback in backend.log at every
    stop — and a hangup stops it as SIGTERM does."""
    p = Proc(SERVE, 0, ready=LISTENING, cwd=fake_main,
             env=_env(fake_main, **database, SAUTIUM_START_TOKEN="a-test-start"))
    try:
        p.send(sig)
        out = p.rest()
    finally:
        p.close()
    assert _said(out) == said, out
    assert "lifespan shutdown" in out and out[-1] == f"exit {ends}", out
    assert not any("Traceback" in line for line in out), out
