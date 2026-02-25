"""Safe execute_query tool for arbitrary SELECT queries.

Allows LLM to run read-only SQL against the music database.
Whitelist approach: only SELECT/WITH allowed.
"""

import logging
import re

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

_conn = None


def _get_conn():
    global _conn
    if _conn is None or _conn.closed:
        _conn = psycopg2.connect(settings.database_url)
        _conn.autocommit = False  # We'll use transaction for READ ONLY
    return _conn


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
        conn = _get_conn()
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SET TRANSACTION READ ONLY")
            cur.execute("SET LOCAL statement_timeout = '10s'")
            cur.execute(sql)
            rows = [dict(r) for r in cur.fetchall()]
            conn.commit()  # end transaction

        if not rows:
            return "Query returned 0 rows."

        # Format as text table
        return _format_table(rows)

    except psycopg2.Error as e:
        try:
            _get_conn().rollback()
        except Exception:
            pass
        error_msg = str(e).strip().split("\n")[0]
        return f"SQL Error: {error_msg}"
    except Exception as e:
        try:
            _get_conn().rollback()
        except Exception:
            pass
        return f"Error: {e}"


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
