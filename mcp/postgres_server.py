#!/usr/bin/env python3
"""The assistant's SQL tool: one read-only statement per call against the
library database, its rows back as JSON.

It lives in the tree, so what runs is what the release carries, the same on
the launcher and in Docker; the server it replaced was an npm package
resolved by name at every agent spawn.

One statement: the text is opened as a cursor inside pg_temp.sautium_query
(`OPEN … FOR EXECUTE`), which PostgreSQL does only for a single statement
that returns rows, in a READ ONLY transaction under a statement timeout. At
most ROW_CAP rows and CHAR_BUDGET characters of them leave the database —
the function fetches one row at a time and counts them, and a row too wide
for what is left is cut before its JSON is made — each row as PostgreSQL
wrote its JSON: numbers exact, every column there, two of one name included.
An error's words are cut to the budget too. Nothing of a call stays in its
session: the transaction rolls back — what the read-only rule lets run,
EXPLAIN ANALYZE of CREATE TABLE AS, goes with it — and a session-level
advisory lock it took, which a rollback keeps, is released. What the
connection may read or do at all is its role's business — its grants — not
this file's: a superuser's server functions act outside any transaction (a
replication slot made), as does any role's on the sessions of its own role
(one ended), and a rollback undoes none of that. The role needs TEMP on the
database for the function.

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
# the row cap does. Below the 100,000 characters at which Claude Code, by
# default, cuts an MCP answer of its own accord.
CHAR_BUDGET = 80_000

# A session's own function: created where the role may write (pg_temp), once
# per connection, in a transaction of its own that is writable whatever the
# role's default. It fetches one row at a time (a FOR loop prefetches them by
# tens), returns the rows that fit — the array's brackets and commas counted
# — and, when more matched, one last row naming what cut them: 'rows' (the
# cap) or 'size' (the budget). A row whose stored size is past eight times
# what is left of the budget is cut before its JSON is made: no text renders
# that much smaller than its storage.
_QUERY_FUNCTION = """
CREATE FUNCTION pg_temp.sautium_query(q text, cap integer, budget integer)
RETURNS TABLE (answer_row text, cut text)
LANGUAGE plpgsql AS $$
DECLARE
    curs refcursor;
    r record;
    n integer := 0;
    used bigint := 2;
BEGIN
    OPEN curs FOR EXECUTE q;
    LOOP
        FETCH curs INTO r;
        EXIT WHEN NOT FOUND;
        IF n = cap THEN
            answer_row := NULL; cut := 'rows'; RETURN NEXT; RETURN;
        END IF;
        IF pg_column_size(r) > 8 * (budget - used) + 1024 THEN
            answer_row := NULL; cut := 'size'; RETURN NEXT; RETURN;
        END IF;
        answer_row := row_to_json(r)::text;
        used := used + length(answer_row) + CASE WHEN n > 0 THEN 1 ELSE 0 END;
        IF used > budget THEN
            answer_row := NULL; cut := 'size'; RETURN NEXT; RETURN;
        END IF;
        n := n + 1;
        cut := NULL;
        RETURN NEXT;
    END LOOP;
END
$$
"""

mcp = FastMCP("Sautium SQL", instructions="Read-only SQL over the music library database.")

_conn = None


def _connection():
    global _conn
    if _conn is None or _conn.closed:
        try:
            conn = psycopg2.connect(connect_timeout=10, **DB)
        except psycopg2.OperationalError as e:
            raise ValueError(f"the database is not reachable: {str(e).strip()}") from None
        try:
            with conn.cursor() as cur:
                cur.execute("SET TRANSACTION READ WRITE")
                cur.execute(_QUERY_FUNCTION)
            conn.commit()
        except psycopg2.Error as e:
            conn.close()
            raise ValueError("the tool's session function could not be made: "
                             f"{(e.pgerror or str(e)).strip()}") from None
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
    """The rows of one read-only statement, as a JSON array; a note comes
    first when more matched than ROW_CAP or CHAR_BUDGET let through. Raises
    ValueError with the database's own words on an error."""
    text = sql.strip().rstrip(";").strip()
    conn = _connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SET TRANSACTION READ ONLY")
            cur.execute("SET LOCAL statement_timeout = %s", (TIMEOUT_MS,))
            cur.execute("SELECT answer_row, cut FROM pg_temp.sautium_query(%s, %s, %s)",
                        (text, ROW_CAP, CHAR_BUDGET))
            rows = cur.fetchall()
    except psycopg2.Error as e:
        # PostgreSQL quotes a value it could not read whole, however long
        said = (e.pgerror or str(e)).strip()
        raise ValueError(said if len(said) <= CHAR_BUDGET
                         else said[:CHAR_BUDGET] + " … (cut)") from None
    finally:
        _end(conn)
    kept = [row for row, cut in rows if cut is None]
    cut = rows[-1][1] if rows else None
    answer = "[" + ",".join(kept) + "]"
    # The note first: Claude Code may cut a long answer's tail itself
    if cut == "size":
        answer = (f"(the first {len(kept)} rows, all an answer may carry; "
                  "select fewer or narrower columns, or fewer rows)\n" + answer)
    elif cut == "rows":
        answer = f"(the first {ROW_CAP} rows; narrow the query or add a LIMIT)\n" + answer
    return answer


@mcp.tool()
def query(sql: str) -> str:
    """Run one read-only SQL statement (SELECT, WITH … SELECT, VALUES, TABLE,
    EXPLAIN, SHOW) against the music library database and return its rows as
    JSON. One statement per call; at most 500 rows, and about 80,000
    characters, come back."""
    return run_query(sql)


def main():
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
