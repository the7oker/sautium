"""Persist the libtorrent DHT routing table between runs.

libtorrent hands its DHT state out as a bencoded session_params buffer; the
latest snapshot lives in the node's own database (p2p_dht_state, one row)
so the next session rejoins the DHT from its last neighbours instead of the
public bootstrap routers: seconds instead of a 30 s bootstrap, and a way in
where UDP to those routers is throttled. Both runtimes use this — the
launcher through its local DSN, the Docker backend through its own.

Called off the event loop (executor). One short-lived connection per call:
a call happens at most once per re-announce cycle, never on a hot path.
"""

from __future__ import annotations

from typing import Optional

import psycopg2


class DhtStateStore:
    def __init__(self, dsn: str):
        self._dsn = dsn

    def load(self) -> Optional[bytes]:
        conn = psycopg2.connect(self._dsn)
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT state FROM p2p_dht_state WHERE id = 1")
                row = cur.fetchone()
                return bytes(row[0]) if row and row[0] else None
        finally:
            conn.close()

    def save(self, buf: bytes) -> None:
        conn = psycopg2.connect(self._dsn)
        conn.autocommit = True
        try:
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
        finally:
            conn.close()
