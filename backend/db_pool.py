"""
Thread-safe database connection pool for raw SQL queries.

Replaces per-module psycopg2 singleton connections with a shared
ThreadedConnectionPool. Each call gets its own connection from the pool,
ensuring thread safety across concurrent requests and background threads.

And Listener, the one way a backend module hears NOTIFY: a thread of its
own on a connection of its own, stopped at once.
"""

import atexit
import logging
import selectors
import socket
import threading
from contextlib import contextmanager
from typing import Callable, Optional

import psycopg2
import psycopg2.extensions
import psycopg2.extras
import psycopg2.pool

from config import settings

logger = logging.getLogger(__name__)

_pool: Optional[psycopg2.pool.ThreadedConnectionPool] = None

# psycopg2 keeps only `minconn` idle connections and CLOSES every other one
# on putconn, so a burst (the Home screen fires nine queries at once) above
# this count opens a fresh server backend per request and pays the whole
# connect each time. Sized for that burst plus the background workers;
# MAX_CONNECTIONS is the ceiling under load.
IDLE_CONNECTIONS = 8
MAX_CONNECTIONS = 20


def _get_pool() -> psycopg2.pool.ThreadedConnectionPool:
    global _pool
    if _pool is None or _pool.closed:
        _pool = psycopg2.pool.ThreadedConnectionPool(
            minconn=IDLE_CONNECTIONS,
            maxconn=MAX_CONNECTIONS,
            dsn=settings.database_url,
            options="-c timezone=UTC",
        )
        logger.info("Database connection pool initialized (%d-%d connections)",
                    IDLE_CONNECTIONS, MAX_CONNECTIONS)
    return _pool


def close_pool():
    global _pool
    if _pool and not _pool.closed:
        _pool.closeall()
        logger.info("Database connection pool closed")
        _pool = None


atexit.register(close_pool)


@contextmanager
def get_conn():
    """Get a connection from the pool. Returns to pool on exit."""
    pool = _get_pool()
    conn = pool.getconn()
    try:
        conn.autocommit = True
        yield conn
    finally:
        pool.putconn(conn)


@contextmanager
def transaction(cursor_factory=None):
    """A cursor on a pooled connection inside ONE transaction: committed when
    the block ends, rolled back when it raises. The connection goes back to
    the pool in autocommit — the pool's contract with every other caller."""
    with get_conn() as conn:
        conn.autocommit = False
        try:
            with conn.cursor(cursor_factory=cursor_factory) as cur:
                yield cur
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.autocommit = True


def db_query(sql: str, params=None) -> list[dict]:
    """Execute a SELECT query, return list of dicts."""
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, params)
            return [dict(row) for row in cur.fetchall()]


def db_query_one(sql: str, params=None) -> Optional[dict]:
    """Execute a SELECT query, return first row as dict or None."""
    rows = db_query(sql, params)
    return rows[0] if rows else None


def db_query_with_ef_search(sql: str, params, ef_search: int) -> list[dict]:
    """
    Run a SELECT with hnsw.ef_search raised for the statement (pgvector's
    default 40 is "single nearest match" tuning); iterative_scan keeps the
    graph walk going when post-scan filters eat into the first wave.

    Pool connections are autocommit; SET LOCAL only takes effect inside an
    explicit transaction, so toggle autocommit off, set the GUCs, run the
    query, commit, and restore the connection for the next caller.
    """
    with get_conn() as conn:
        conn.autocommit = False
        try:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute("SET LOCAL hnsw.ef_search = %s", (ef_search,))
                cur.execute("SET LOCAL hnsw.iterative_scan = relaxed_order")
                cur.execute(sql, params)
                rows = [dict(r) for r in cur.fetchall()]
            conn.commit()
            return rows
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.autocommit = True


def db_execute(sql: str, params=None) -> Optional[dict]:
    """Execute INSERT/UPDATE/DELETE. Returns first row if RETURNING is used."""
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, params)
            if cur.description:
                row = cur.fetchone()
                return dict(row) if row else None
            return None


class Listener:
    """A thread LISTENing on `channels` until stop(). `on_notify(notifies)`
    runs on it with every NOTIFY that arrived since its last call — a burst
    that lands during one call is cleared together — and `on_connect()` on
    each (re)connect, since a NOTIFY reaches only a live listener: what was
    sent while the connection was down is swept there.

    It blocks on its connection and on a wake socket, never on a timeout:
    stop() closes the socket's other end and the thread ends at once. The
    flag each listener used to look at between 1-5 s selects kept the
    backend's stop waiting on them in turn, five seconds in all on a Mac
    (2026-10-25). Through the selectors module's default, as every wait on
    HQPlayer (hqplayer_client.wait_on): select.select refuses a descriptor
    past 1023. Keepalives on the connection: an idle socket reaped without
    an RST would swallow every later NOTIFY. A connection that fails is said
    once, then quietly retried every RETRY_S until it holds; a handler that
    fails is said (the database's trouble as a warning, a bug with its
    traceback), and the connection, which did nothing wrong, listens on —
    but for the sweep, which is retried on a connection made again.
    """

    RETRY_S = 2
    # Busy when its stop comes — in a handler, or in a connect: the stop waits
    # this long, then leaves it to end (the thread is a daemon) and says so
    STOP_WAIT_S = 2

    def __init__(self, name: str, channels: tuple, on_notify: Callable[[list], None],
                 on_connect: Optional[Callable[[], None]] = None) -> None:
        self.name = name
        self.channels = channels
        self.on_notify = on_notify
        self.on_connect = on_connect
        self._thread: Optional[threading.Thread] = None
        self._wake: Optional[socket.socket] = None

    def start(self) -> None:
        if self._thread is not None:
            return
        self._wake, heard = socket.socketpair()
        self._thread = threading.Thread(target=self._run, args=(heard,), daemon=True,
                                        name=self.name)
        self._thread.start()

    def stop(self) -> None:
        thread, wake = self._thread, self._wake
        if thread is None:
            return
        self._thread = self._wake = None
        wake.close()
        thread.join(self.STOP_WAIT_S)
        if thread.is_alive():
            logger.warning("%s: still busy %g s after the stop", self.name, self.STOP_WAIT_S)

    def _run(self, wake: socket.socket) -> None:
        failing = False
        with selectors.DefaultSelector() as sel:
            sel.register(wake, selectors.EVENT_READ)
            try:
                while True:
                    conn = None
                    try:
                        conn = psycopg2.connect(settings.database_url, keepalives=1,
                                                keepalives_idle=30, keepalives_interval=10,
                                                keepalives_count=3)
                        conn.set_isolation_level(psycopg2.extensions.ISOLATION_LEVEL_AUTOCOMMIT)
                        with conn.cursor() as cur:
                            for channel in self.channels:
                                cur.execute(f"LISTEN {channel}")
                        if failing:
                            logger.info("%s: listening again", self.name)
                            failing = False
                        if sel.select(0):            # stopped while it connected
                            return
                        # The sweep is the connect's last step: one that
                        # failed is made again on a new connection, or what
                        # came while it was down waits for the next NOTIFY
                        if self._call(f"its sweep failed — swept again on a connection made in "
                                      f"{self.RETRY_S:g} s", self.on_connect):
                            # By its number: a connection psycopg2 found broken
                            # refuses fileno(), and the key would stay behind
                            fd = conn.fileno()
                            sel.register(fd, selectors.EVENT_READ)
                            try:
                                while not any(key.fileobj is wake for key, _ in sel.select()):
                                    conn.poll()
                                    if conn.notifies:
                                        notifies = list(conn.notifies)
                                        conn.notifies.clear()
                                        self._call("its handler failed", self.on_notify, notifies)
                            finally:
                                sel.unregister(fd)
                            return
                    except psycopg2.Error as e:
                        if sel.select(0):            # stopped meanwhile: nothing to retry or say
                            return
                        if not failing:
                            logger.warning("%s: %s — retried every %g s", self.name,
                                           str(e).strip(), self.RETRY_S)
                        failing = True
                    except Exception:
                        if sel.select(0):
                            return
                        logger.exception("%s failed — listening again in %g s", self.name,
                                         self.RETRY_S)
                    finally:
                        if conn is not None:
                            conn.close()
                    if sel.select(self.RETRY_S):
                        return
            finally:
                wake.close()

    def _call(self, failed: str, handler, *args) -> bool:
        """`handler(*args)`; False when it failed, said: the database's
        trouble as a warning — a connection the pool held went with a
        PostgreSQL restart — any other with its traceback."""
        if handler is None:
            return True
        try:
            handler(*args)
            return True
        except (psycopg2.OperationalError, psycopg2.InterfaceError) as e:
            logger.warning("%s: %s: %s", self.name, failed, str(e).strip())
            return False
        except Exception:
            logger.exception("%s: %s", self.name, failed)
            return False
