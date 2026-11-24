"""Persist the libtorrent DHT routing table between runs.

libtorrent hands its DHT state out as a bencoded session_params buffer; the
latest snapshot lives in the node's own database (p2p_dht_state, one row)
so the next session rejoins the DHT from its last neighbours instead of the
public bootstrap routers: seconds instead of a 30 s bootstrap, and a way in
where UDP to those routers is throttled. Both runtimes use this — the
launcher through its local DSN, the Docker backend through its own.

Called off the event loop (executor), and once on it, at the service's stop.

One connection, held from the first call until `close()` (since 2026-11-24,
PI-NODE-2D §8.3). The save that matters most is the last one, at the node's
stop — and a PostgreSQL that has been told to stop takes no new connection:
its smart shutdown (SIGTERM, the compose files' stop signal) serves the
clients it has until they leave and refuses every other. A connection opened
for that save was refused ("the database system is shutting down"), and the
routing table of the whole run was lost with "DHT: state save failed". The
store opens its connection at the start's load and saves through it. A
connection that went meanwhile (PostgreSQL restarted under a running node)
is found by the next periodic save and made again there, while a new
connection is still taken.
"""

from __future__ import annotations

import threading
from typing import Callable, Optional

import psycopg2


class DhtStateStore:
    def __init__(self, dsn: str):
        self._dsn = dsn
        self._conn = None
        # load and the periodic save run on executor threads, the stop's
        # save on the loop's: one statement at a time on the one connection
        self._lock = threading.Lock()

    def _held(self):
        if self._conn is None or self._conn.closed:
            self._conn = psycopg2.connect(self._dsn)
            self._conn.autocommit = True
        return self._conn

    def _drop(self) -> None:
        conn, self._conn = self._conn, None
        if conn is not None:
            conn.close()

    def _on_connection(self, work: Callable):
        with self._lock:
            held = self._conn is not None and not self._conn.closed
            try:
                return work(self._held())
            except (psycopg2.OperationalError, psycopg2.InterfaceError):
                self._drop()
                if not held:
                    raise
                # The held connection was cut, which psycopg2 learns at the
                # statement: the same work once on a connection made now. A
                # PostgreSQL that is stopping refuses that one, and the
                # failure is then the caller's to say.
                return work(self._held())

    def load(self) -> Optional[bytes]:
        def read(conn):
            with conn.cursor() as cur:
                cur.execute("SELECT state FROM p2p_dht_state WHERE id = 1")
                row = cur.fetchone()
                return bytes(row[0]) if row and row[0] else None
        return self._on_connection(read)

    def save(self, buf: bytes) -> None:
        def write(conn):
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO p2p_dht_state (id, state, saved_at)
                    VALUES (1, %s, now())
                    ON CONFLICT (id) DO UPDATE
                        SET state = EXCLUDED.state, saved_at = EXCLUDED.saved_at
                    """,
                    (psycopg2.Binary(buf),),
                )
        self._on_connection(write)

    def close(self) -> None:
        """After the stop's save: the connection let go, so that a
        PostgreSQL waiting for its clients waits for this one no longer. A
        later call opens it again."""
        with self._lock:
            self._drop()
