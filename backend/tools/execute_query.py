"""Safe execute_query tool for arbitrary SELECT queries.

Allows LLM to run read-only SQL against the music database. What bounds it
is the login: the assistant role (desktop/db_init.ASSISTANT_ROLE) reads the
catalogue and nothing else, and writes and creates nothing. The SELECT/WITH
check, the read-only transaction and the timeout are a second layer — the
session may change its own settings, so a statement past the deadline is
cancelled from here.
"""

import logging
import re
import threading
from typing import Optional

import psycopg2
import psycopg2.extras

from config import settings

logger = logging.getLogger(__name__)

# Forbidden keywords (case-insensitive)
_FORBIDDEN = re.compile(
    r'\b(INSERT|UPDATE|DELETE|DROP|ALTER|CREATE|TRUNCATE|GRANT|REVOKE|'
    r'COPY|EXECUTE|CALL|DO|VACUUM|REINDEX|CLUSTER|COMMENT|SECURITY|'
    r'SET\s+ROLE|SET\s+SESSION)\b',
    re.IGNORECASE,
)

# Only allow statements starting with SELECT or WITH
_ALLOWED_START = re.compile(r'^\s*(SELECT|WITH)\b', re.IGNORECASE)

# Past this a statement is cancelled by the client; the server-side
# statement_timeout below is the session's to change.
_DEADLINE_SECONDS = 15

# One connection, kept (the persistent-connection rule), used by one query
# at a time: the API providers' turns share it.
_conn = None
_pid: Optional[int] = None
_lock = threading.Lock()


def connection_pid() -> Optional[int]:
    """The server process of the tool's connection while it has one — the
    session claude_code_runner leaves open when it ends the assistant's."""
    return _pid


def _get_conn():
    global _conn, _pid
    if _conn is None or _conn.closed:
        _conn = psycopg2.connect(settings.assistant_database_url)
        _conn.autocommit = False  # We'll use transaction for READ ONLY
        _pid = _conn.get_backend_pid()
    return _conn


def _reset(conn) -> None:
    """End the query's transaction and clear what it left on the session —
    advisory locks, settings, temporary tables, LISTENs: the connection
    outlives the query, and the next one must not inherit it."""
    conn.rollback()
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute("DISCARD ALL")
    finally:
        conn.autocommit = False


def execute_query(sql: str) -> str:
    """Execute a read-only SQL query and return formatted results.

    Args:
        sql: SQL SELECT query to execute

    Returns:
        Formatted text table with results
    """
    sql = sql.strip().rstrip(";")

    # Validate: must start with SELECT or WITH
    if not _ALLOWED_START.match(sql):
        return "Error: Only SELECT and WITH queries are allowed."

    # Validate: no forbidden keywords
    if _FORBIDDEN.search(sql):
        return "Error: Query contains forbidden keywords. Only read-only SELECT queries are allowed."

    # Add LIMIT if not present
    if not re.search(r'\bLIMIT\b', sql, re.IGNORECASE):
        sql += " LIMIT 100"

    try:
        with _lock:
            conn = _get_conn()
            deadline = threading.Timer(_DEADLINE_SECONDS, conn.cancel)
            deadline.start()
            try:
                with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                    cur.execute("SET TRANSACTION READ ONLY")
                    cur.execute("SET LOCAL statement_timeout = '10s'")
                    cur.execute(sql)
                    rows = [dict(r) for r in cur.fetchall()]
            finally:
                deadline.cancel()
                _reset(conn)
    except psycopg2.Error as e:
        error_msg = str(e).strip().split("\n")[0]
        return f"SQL Error: {error_msg}"

    if not rows:
        return "Query returned 0 rows."

    # Format as text table
    return _format_table(rows)


def _format_table(rows: list[dict]) -> str:
    """Format query results as a readable text table."""
    if not rows:
        return "No results."

    columns = list(rows[0].keys())

    # Calculate column widths
    widths = {col: len(col) for col in columns}
    for row in rows:
        for col in columns:
            val = str(row.get(col, ""))
            if len(val) > 80:
                val = val[:77] + "..."
            widths[col] = max(widths[col], len(val))

    # Cap total width
    total = sum(widths.values()) + (len(columns) - 1) * 3
    if total > 200:
        # Shrink widest columns
        max_col_width = 40
        widths = {col: min(w, max_col_width) for col, w in widths.items()}

    # Header
    header = " | ".join(col.ljust(widths[col])[:widths[col]] for col in columns)
    sep = "-+-".join("-" * widths[col] for col in columns)

    lines = [header, sep]

    for row in rows:
        vals = []
        for col in columns:
            val = str(row.get(col, ""))
            if len(val) > widths[col]:
                val = val[:widths[col] - 3] + "..."
            vals.append(val.ljust(widths[col])[:widths[col]])
        lines.append(" | ".join(vals))

    lines.append(f"\n({len(rows)} rows)")
    return "\n".join(lines)
