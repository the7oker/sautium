"""db_pool.Listener, the one way a backend module hears NOTIFY.

Seven modules each ran a LISTEN loop of their own that looked at a stop
flag between 1-5 s selects, and four of them were joined one after another
when the backend stopped: five seconds of a Mac's stop on 2026-10-25. The
listener blocks on its connection and a wake socket instead, so a stop ends
it at once. Real PostgreSQL (the test cluster) and real threads.
"""

import logging
import os
import secrets
import sys
import threading
import time

import psycopg2
import pytest

from conftest import PG
from db_pool import Listener


@pytest.fixture
def channel(monkeypatch):
    """A channel of this test's own on the cluster's maintenance database,
    which config.settings — the listener's DSN — now names."""
    try:
        psycopg2.connect(dbname="postgres", connect_timeout=3, **PG).close()
    except psycopg2.OperationalError:
        pytest.skip("no PostgreSQL")
    from config import settings
    for key, value in dict(postgres_host=PG["host"], postgres_port=PG["port"],
                           postgres_user=PG["user"], postgres_password=PG["password"],
                           postgres_db="postgres").items():
        monkeypatch.setattr(settings, key, value)
    monkeypatch.setattr(Listener, "RETRY_S", 0.1)
    return f"test_listener_{secrets.token_hex(4)}"


@pytest.fixture
def notify(channel):
    conn = psycopg2.connect(dbname="postgres", **PG)
    conn.autocommit = True

    def send(payload: str = "") -> None:
        with conn.cursor() as cur:
            cur.execute("SELECT pg_notify(%s, %s)", (channel, payload))
    yield send
    conn.close()


def _until(cond, within: float = 10) -> None:
    deadline = time.monotonic() + within
    while not cond():
        assert time.monotonic() < deadline, "it never came"
        time.sleep(0.01)


class Heard:
    """What a listener's handlers were called with, in order."""

    def __init__(self):
        self.connects = 0
        self.calls = []

    def on_connect(self):
        self.connects += 1

    def on_notify(self, notifies):
        self.calls.append([n.payload for n in notifies])

    @property
    def payloads(self):
        return [p for call in self.calls for p in call]


def _listening(channel: str) -> list:
    """The backend pids LISTENing on `channel`, by the statement they ran last."""
    with psycopg2.connect(dbname="postgres", **PG) as conn, conn.cursor() as cur:
        cur.execute("SELECT pid FROM pg_stat_activity WHERE query = %s", (f"LISTEN {channel}",))
        return [pid for (pid,) in cur.fetchall()]


def _messages(caplog, level):
    return [r.getMessage() for r in caplog.records if r.name == "db_pool" and r.levelno == level]


def test_a_stop_ends_the_listener_at_once(channel):
    heard = Heard()
    listener = Listener("test-listener", (channel,), heard.on_notify, heard.on_connect)
    listener.start()
    _until(lambda: heard.connects == 1)
    thread = listener._thread
    began = time.monotonic()
    listener.stop()
    assert time.monotonic() - began < 0.5
    assert not thread.is_alive()
    _until(lambda: not _listening(channel))      # its connection closed with it


def test_notifies_reach_the_handler_in_order_and_a_burst_folds(channel, notify):
    held, release = threading.Event(), threading.Event()
    heard = Heard()

    def on_notify(notifies):
        heard.on_notify(notifies)
        held.set()
        assert release.wait(10)
    listener = Listener("test-listener", (channel,), on_notify, heard.on_connect)
    listener.start()
    try:
        _until(lambda: heard.connects == 1)
        notify("1")
        assert held.wait(10)
        for payload in "2345":
            notify(payload)
        time.sleep(0.2)                          # the burst lands while the handler runs
        release.set()
        _until(lambda: len(heard.payloads) == 5)
    finally:
        release.set()
        listener.stop()
    assert heard.payloads == ["1", "2", "3", "4", "5"]
    assert len(heard.calls) < 5


def test_a_dropped_connection_is_said_once_and_listened_again(channel, notify, caplog):
    heard = Heard()
    listener = Listener("test-listener", (channel,), heard.on_notify, heard.on_connect)
    with caplog.at_level(logging.INFO, logger="db_pool"):
        listener.start()
        try:
            _until(lambda: heard.connects == 1)
            with psycopg2.connect(dbname="postgres", **PG) as conn, conn.cursor() as cur:
                cur.execute("SELECT pg_terminate_backend(%s)", (_listening(channel)[0],))
            _until(lambda: heard.connects == 2)      # what was sent meanwhile is swept here
            _until(lambda: len(_listening(channel)) == 1)
            notify("after")
            _until(lambda: heard.payloads == ["after"])
        finally:
            listener.stop()
    assert len(_messages(caplog, logging.WARNING)) == 1
    assert _messages(caplog, logging.INFO) == ["test-listener: listening again"]
    assert _messages(caplog, logging.ERROR) == []


def test_a_connection_that_keeps_failing_is_said_once(channel, monkeypatch, caplog):
    from config import settings
    monkeypatch.setattr(settings, "postgres_port", 1)          # refused at once
    heard = Heard()
    attempts = []
    real_connect = psycopg2.connect
    monkeypatch.setattr(psycopg2, "connect", lambda *a, **kw: attempts.append(1) or real_connect(*a, **kw))
    listener = Listener("test-listener", (channel,), heard.on_notify, heard.on_connect)
    with caplog.at_level(logging.DEBUG, logger="db_pool"):
        listener.start()
        try:
            _until(lambda: len(attempts) >= 3)
            monkeypatch.setattr(settings, "postgres_port", PG["port"])
            _until(lambda: heard.connects == 1)
        finally:
            listener.stop()
    assert len(_messages(caplog, logging.WARNING)) == 1
    assert _messages(caplog, logging.INFO) == ["test-listener: listening again"]


@pytest.mark.parametrize("raised", [RuntimeError("a handler's own bug"),
                                    psycopg2.OperationalError("its own query's database error")],
                         ids=["a-bug", "a-database-error"])
def test_a_handler_that_fails_is_said_and_its_connection_listens_on(channel, notify, caplog, raised):
    """A database error in a handler is the handler's, not the listening
    connection's: dropped and made again, the connection would lose what was
    sent meanwhile on a channel with no sweep."""
    heard = Heard()

    def on_notify(notifies):
        heard.on_notify(notifies)
        if len(heard.calls) == 1:
            raise raised
    listener = Listener("test-listener", (channel,), on_notify, heard.on_connect)
    with caplog.at_level(logging.INFO, logger="db_pool"):
        listener.start()
        try:
            _until(lambda: heard.connects == 1)
            notify("first")
            _until(lambda: heard.calls)
            notify("second")
            _until(lambda: heard.payloads == ["first", "second"])
        finally:
            listener.stop()
    assert heard.connects == 1
    failed = [r for r in caplog.records if r.name == "db_pool" and r.levelno == logging.ERROR]
    assert [r.getMessage() for r in failed] == ["test-listener: its handler failed"]
    assert failed[0].exc_info[1] is raised
    assert _messages(caplog, logging.WARNING) == []


def test_a_failure_beside_the_connection_and_its_handlers_is_said_and_retried(channel, monkeypatch,
                                                                            caplog):
    heard = Heard()
    real_connect, calls = psycopg2.connect, []

    def connect(*a, **kw):
        calls.append(1)
        if len(calls) == 1:
            raise ValueError("not psycopg2's")
        return real_connect(*a, **kw)
    monkeypatch.setattr(psycopg2, "connect", connect)
    listener = Listener("test-listener", (channel,), heard.on_notify, heard.on_connect)
    with caplog.at_level(logging.INFO, logger="db_pool"):
        listener.start()
        try:
            _until(lambda: heard.connects == 1)
        finally:
            listener.stop()
    failed = [r for r in caplog.records if r.name == "db_pool" and r.levelno == logging.ERROR]
    assert [r.getMessage() for r in failed] == ["test-listener failed — listening again in 0.1 s"]
    assert failed[0].exc_info[0] is ValueError


def test_a_stop_during_a_connect_runs_no_on_connect(channel, monkeypatch, caplog):
    monkeypatch.setattr(Listener, "STOP_WAIT_S", 0.3)
    connecting, release = threading.Event(), threading.Event()
    real_connect = psycopg2.connect

    def slow_connect(*a, **kw):
        connecting.set()
        assert release.wait(10)
        return real_connect(*a, **kw)
    monkeypatch.setattr(psycopg2, "connect", slow_connect)
    heard = Heard()
    listener = Listener("test-listener", (channel,), heard.on_notify, heard.on_connect)
    listener.start()
    thread = listener._thread
    assert connecting.wait(10)
    with caplog.at_level(logging.WARNING, logger="db_pool"):
        listener.stop()
    release.set()
    thread.join(5)
    assert not thread.is_alive()
    assert heard.connects == 0                   # nothing runs after the stop has returned
    assert _messages(caplog, logging.WARNING) == ["test-listener: still busy 0.3 s after the stop"]
    _until(lambda: not _listening(channel))


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX descriptors")
def test_a_connection_past_descriptor_1023_is_heard(channel, notify):
    """select.select refuses a descriptor past 1023: the listener would have
    gone deaf on a process holding that many."""
    import resource
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    want = 1200
    if soft < want:
        if hard != resource.RLIM_INFINITY and hard < want:
            pytest.skip(f"the descriptor limit stops at {hard}")
        resource.setrlimit(resource.RLIMIT_NOFILE, (want, hard))
    held = []
    try:
        while not held or held[-1] < 1030:
            held.append(os.open(os.devnull, os.O_RDONLY))
        heard = Heard()
        listener = Listener("test-listener", (channel,), heard.on_notify, heard.on_connect)
        listener.start()
        try:
            _until(lambda: heard.connects == 1)
            notify("high")
            _until(lambda: heard.payloads == ["high"])
        finally:
            listener.stop()
    finally:
        for fd in held:
            os.close(fd)
        resource.setrlimit(resource.RLIMIT_NOFILE, (soft, hard))


def test_a_stop_waits_for_a_busy_handler_only_so_long(channel, notify, monkeypatch, caplog):
    monkeypatch.setattr(Listener, "STOP_WAIT_S", 0.3)
    busy, release = threading.Event(), threading.Event()
    heard = Heard()

    def on_notify(notifies):
        busy.set()
        assert release.wait(10)
    listener = Listener("test-listener", (channel,), on_notify, heard.on_connect)
    listener.start()
    _until(lambda: heard.connects == 1)
    thread = listener._thread
    notify()
    assert busy.wait(10)
    with caplog.at_level(logging.WARNING, logger="db_pool"):
        began = time.monotonic()
        listener.stop()
        took = time.monotonic() - began
    try:
        assert 0.3 <= took < 1
        assert _messages(caplog, logging.WARNING) == ["test-listener: still busy 0.3 s after the stop"]
    finally:
        release.set()
    thread.join(5)
    assert not thread.is_alive()                 # it ends by itself once the handler returns


def test_a_sweep_that_fails_is_made_again_on_a_new_connection(channel, notify, caplog):
    """The sweep is the connect's last step: one that failed — the pool's
    connections stale after a PostgreSQL restart — and was left so, would
    leave what came meanwhile waiting for the next NOTIFY of its kind."""
    heard = Heard()

    def on_connect():
        heard.on_connect()
        if heard.connects == 1:
            raise psycopg2.OperationalError("server closed the connection unexpectedly")
    listener = Listener("test-listener", (channel,), heard.on_notify, on_connect)
    with caplog.at_level(logging.INFO, logger="db_pool"):
        listener.start()
        try:
            _until(lambda: heard.connects == 2)
            _until(lambda: len(_listening(channel)) == 1)
            notify("after")
            _until(lambda: heard.payloads == ["after"])
        finally:
            listener.stop()
    failed = [r for r in caplog.records if r.name == "db_pool" and r.levelno == logging.ERROR]
    assert [r.getMessage() for r in failed] == [
        "test-listener: its sweep failed — swept again on a connection made in 0.1 s"]
    assert failed[0].exc_info[0] is psycopg2.OperationalError


@pytest.mark.parametrize("raised", [psycopg2.OperationalError("the database is going away"),
                                    ValueError("not psycopg2's")], ids=["psycopg2", "other"])
def test_a_stop_during_a_connect_that_fails_says_nothing_after_it(channel, monkeypatch, caplog, raised):
    monkeypatch.setattr(Listener, "STOP_WAIT_S", 0.3)
    connecting, release = threading.Event(), threading.Event()

    def failing_connect(*a, **kw):
        connecting.set()
        assert release.wait(10)
        raise raised
    monkeypatch.setattr(psycopg2, "connect", failing_connect)
    listener = Listener("test-listener", (channel,), lambda notifies: None)
    listener.start()
    thread = listener._thread
    assert connecting.wait(10)
    with caplog.at_level(logging.WARNING, logger="db_pool"):
        listener.stop()
        release.set()
        thread.join(5)
    assert not thread.is_alive()
    said = [r.getMessage() for r in caplog.records if r.name == "db_pool" and r.levelno >= logging.WARNING]
    assert said == ["test-listener: still busy 0.3 s after the stop"]
