"""
Thread-safe database connection pool for raw SQL queries.

Replaces per-module psycopg2 singleton connections with a shared
ThreadedConnectionPool. Each call gets its own connection from the pool,
ensuring thread safety across concurrent requests and background threads.
"""

import atexit
import logging
from contextlib import contextmanager
from typing import Optional

import psycopg2
import psycopg2.extras
import psycopg2.pool

from config import settings

logger = logging.getLogger(__name__)

_pool: Optional[psycopg2.pool.ThreadedConnectionPool] = None


def _get_pool() -> psycopg2.pool.ThreadedConnectionPool:
    global _pool
    if _pool is None or _pool.closed:
        _pool = psycopg2.pool.ThreadedConnectionPool(
            minconn=2,
            maxconn=20,
            dsn=settings.database_url,
            options="-c timezone=UTC",
        )
        logger.info("Database connection pool initialized (2-20 connections)")
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


def db_execute(sql: str, params=None) -> Optional[dict]:
    """Execute INSERT/UPDATE/DELETE. Returns first row if RETURNING is used."""
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, params)
            if cur.description:
                row = cur.fetchone()
                return dict(row) if row else None
            return None
