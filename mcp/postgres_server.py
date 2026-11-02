#!/usr/bin/env python3
"""The assistant's SQL tool: one read-only statement per call against the
library database, its rows back as JSON.

It lives in the tree, so what runs is what the release carries, the same on
the launcher and in Docker; the server it replaced was an npm package
resolved by name at every agent spawn.

One statement: the text is opened as a cursor inside pg_temp.sautium_query
(`FOR … IN EXECUTE`), which PostgreSQL does only for a single statement that
returns rows, in a READ ONLY transaction under a statement timeout. At most
ROW_CAP rows and CHAR_BUDGET characters come back, each row as PostgreSQL
wrote its JSON: numbers exact, every column there, two of one name included.
Nothing of a call outlives it: the transaction rolls back — what the
read-only rule lets run, EXPLAIN ANALYZE of CREATE TABLE AS, goes with it —
and a session-level advisory lock it took, which a rollback keeps, is
released. What the connection may read at all is its role's business — its
grants — not this file's; the role needs TEMP on the database for the
function.

All logging goes to stderr (stdout is the STDIO MCP transport).
"""

import logging
import os
import sys

import psycopg2
from mcp.server.fastmcp import FastMCP

logging.basicConfig(
    stream=sys.stderr,
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
logger = logging.getLogger("postgres-mcp")

DB = {
    "host": os.getenv("DB_HOST", "127.0.0.1"),
    "port": os.getenv("DB_PORT", "5432"),
    "user": os.getenv("DB_USER", "musicai"),
    "password": os.getenv("DB_PASSWORD", ""),
    "dbname": os.getenv("DB_NAME", "music_ai"),
}
# A runaway query fails inside the turn, so the model can retry lighter
# instead of spending the chat's wall clock.
TIMEOUT_MS = int(os.getenv("DB_QUERY_TIMEOUT", "30000"))
ROW_CAP = 500
# Wide rows — a lyric, an embedding — fill the agent's context long before
# the row cap does
CHAR_BUDGET = 100_000

# A session's own function: created where the role may write (pg_temp), once
# per connection, in a transaction of its own that is writable whatever the
# role's default. The loop reads one row past the cap, so a cut answer can
# say so.
_QUERY_FUNCTION = """
CREATE FUNCTION pg_temp.sautium_query(q text, cap integer) RETURNS SETOF text
LANGUAGE plpgsql AS $$
DECLARE
    r record;
    n integer := 0;
BEGIN
    FOR r IN EXECUTE q LOOP
        n := n + 1;
        EXIT WHEN n > cap + 1;
        RETURN NEXT row_to_json(r)::text;
    END LOOP;
END
$$
"""

mcp = FastMCP("Sautium SQL", instructions="Read-only SQL over the music library database.")

_conn = None


def _connection():
    global _conn
    if _conn is None or _conn.closed:
        conn = psycopg2.connect(connect_timeout=10, **DB)
        with conn.cursor() as cur:
            cur.execute("SET TRANSACTION READ WRITE")
            cur.execute(_QUERY_FUNCTION)
        conn.commit()
        _conn = conn
    return _conn


def _end(conn) -> None:
    try:
        conn.rollback()
        with conn.cursor() as cur:
            cur.execute("SELECT pg_advisory_unlock_all()")
        conn.rollback()
    except psycopg2.Error as e:
        # A closed session holds no lock either
        logger.warning("the database connection broke (%s); the next query reconnects", e)
        conn.close()


def run_query(sql: str) -> str:
    """The rows of one read-only statement, as a JSON array; a note follows
    when more matched than ROW_CAP or CHAR_BUDGET let through. Raises
    ValueError with the database's own words on an error."""
    text = sql.strip().rstrip(";").strip()
    try:
        conn = _connection()
    except psycopg2.Error as e:
        raise ValueError(f"the database is not reachable: {str(e).strip()}") from None
    try:
        with conn.cursor() as cur:
            cur.execute("SET TRANSACTION READ ONLY")
            cur.execute("SET LOCAL statement_timeout = %s", (TIMEOUT_MS,))
            cur.execute("SELECT q FROM pg_temp.sautium_query(%s, %s) AS q", (text, ROW_CAP))
            rows = [row[0] for row in cur.fetchall()]
    except psycopg2.Error as e:
        raise ValueError((e.pgerror or str(e)).strip()) from None
    finally:
        _end(conn)
    kept, size = [], 2
    for row in rows[:ROW_CAP]:
        if size + len(row) + 1 > CHAR_BUDGET:
            break
        kept.append(row)
        size += len(row) + 1
    answer = "[" + ",".join(kept) + "]"
    if len(kept) < min(len(rows), ROW_CAP):
        answer += (f"\n(the first {len(kept)} rows, all an answer may carry; "
                   "select fewer or narrower columns, or fewer rows)")
    elif len(rows) > ROW_CAP:
        answer += f"\n(the first {ROW_CAP} rows; narrow the query or add a LIMIT)"
    return answer


@mcp.tool()
def query(sql: str) -> str:
    """Run one read-only SQL statement (SELECT, WITH … SELECT, VALUES, TABLE,
    EXPLAIN, SHOW) against the music library database and return its rows as
    JSON. One statement per call; at most 500 rows, and about 100,000
    characters, come back."""
    return run_query(sql)


def main():
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
