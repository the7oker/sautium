"""The assistant's SQL tool (mcp/postgres_server.py): one read-only
statement per call, its rows as JSON, at most ROW_CAP of them, under a
statement timeout. Against a real PostgreSQL: what keeps a statement read
only and alone is the server's own rules, which a fake would not have."""

import importlib.util
import json
from pathlib import Path
from urllib.parse import urlparse

import pytest

REPO = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="module")
def sql(scratch_dsn):
    import psycopg2
    with psycopg2.connect(scratch_dsn) as conn, conn.cursor() as cur:
        cur.execute("CREATE TABLE sql_tool_rows (n integer)")
        cur.execute("INSERT INTO sql_tool_rows SELECT generate_series(1, 3)")
    spec = importlib.util.spec_from_file_location("postgres_server",
                                                  REPO / "mcp" / "postgres_server.py")
    server = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(server)
    dsn = urlparse(scratch_dsn)
    server.DB.update(host=dsn.hostname, port=str(dsn.port), user=dsn.username,
                     password=dsn.password, dbname=dsn.path.lstrip("/"))
    server.check = lambda: _rows(scratch_dsn)
    yield server
    if server._conn is not None:
        server._conn.close()


def _rows(dsn) -> list:
    import psycopg2
    with psycopg2.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute("SELECT n FROM sql_tool_rows ORDER BY n")
        rows = [r[0] for r in cur.fetchall()]
        cur.execute("SELECT to_regclass('sql_tool_other') IS NOT NULL")
        return rows + (["sql_tool_other exists"] if cur.fetchone()[0] else [])


def test_a_select_answers_its_rows_as_json(sql):
    assert json.loads(sql.run_query("SELECT n, n * 2 AS twice FROM sql_tool_rows ORDER BY n;")) == [
        {"n": 1, "twice": 2}, {"n": 2, "twice": 4}, {"n": 3, "twice": 6}]


@pytest.mark.parametrize("statement", [
    "INSERT INTO sql_tool_rows VALUES (4)",
    "UPDATE sql_tool_rows SET n = 0",
    "DELETE FROM sql_tool_rows",
    "WITH gone AS (DELETE FROM sql_tool_rows RETURNING n) SELECT n FROM gone",
    "CREATE TABLE sql_tool_other (n integer)",
])
def test_a_statement_that_writes_is_refused(sql, statement):
    with pytest.raises(ValueError):
        sql.run_query(statement)
    assert sql.check() == [1, 2, 3]


@pytest.mark.parametrize("text", [
    "SELECT 1; INSERT INTO sql_tool_rows VALUES (9)",
    "SELECT 1; COMMIT; DELETE FROM sql_tool_rows",
])
def test_a_second_statement_is_refused_and_does_not_run(sql, text):
    with pytest.raises(ValueError):
        sql.run_query(text)
    assert sql.check() == [1, 2, 3]


def test_a_query_past_the_timeout_is_cancelled(sql, monkeypatch):
    monkeypatch.setattr(sql, "TIMEOUT_MS", 200)
    with pytest.raises(ValueError, match="statement timeout"):
        sql.run_query("SELECT pg_sleep(5)")


def test_at_most_the_cap_comes_back_and_the_answer_says_it_was_cut(sql):
    answer = sql.run_query(f"SELECT generate_series(1, {sql.ROW_CAP + 100}) AS n")
    rows, note = answer.split("\n", 1)
    assert [r["n"] for r in json.loads(rows)] == list(range(1, sql.ROW_CAP + 1))
    assert "first 500 rows" in note
    assert "\n" not in sql.run_query(f"SELECT generate_series(1, {sql.ROW_CAP}) AS n")


def test_an_error_leaves_the_tool_answering(sql):
    with pytest.raises(ValueError, match="sql_tool_nowhere"):
        sql.run_query("SELECT * FROM sql_tool_nowhere")
    assert json.loads(sql.run_query("SELECT count(*) AS c FROM sql_tool_rows")) == [{"c": 3}]
