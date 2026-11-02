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
    "EXPLAIN ANALYZE INSERT INTO sql_tool_rows VALUES (5)",
])
def test_a_statement_that_writes_is_refused(sql, statement):
    with pytest.raises(ValueError):
        sql.run_query(statement)
    assert sql.check() == [1, 2, 3]


def test_a_write_the_read_only_rule_lets_through_leaves_nothing(sql):
    """PostgreSQL runs EXPLAIN ANALYZE of CREATE TABLE AS in a read-only
    transaction; the call's transaction rolls back, and the table with it."""
    sql.run_query("EXPLAIN ANALYZE CREATE TABLE sql_tool_other AS SELECT 1 AS n")
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
    note, rows = answer.split("\n", 1)              # the note first: a CLI may cut the tail
    assert [r["n"] for r in json.loads(rows)] == list(range(1, sql.ROW_CAP + 1))
    assert "first 500 rows" in note
    assert "\n" not in sql.run_query(f"SELECT generate_series(1, {sql.ROW_CAP}) AS n")


def test_an_error_leaves_the_tool_answering(sql):
    with pytest.raises(ValueError, match="sql_tool_nowhere"):
        sql.run_query("SELECT * FROM sql_tool_nowhere")
    assert json.loads(sql.run_query("SELECT count(*) AS c FROM sql_tool_rows")) == [{"c": 3}]


def test_a_session_advisory_lock_ends_with_the_call(sql, scratch_dsn):
    """The node's own jobs wait on advisory locks; one an agent's query took
    must not outlive its call."""
    import psycopg2
    sql.run_query("SELECT pg_advisory_lock(424242)")
    with psycopg2.connect(scratch_dsn) as conn, conn.cursor() as cur:
        cur.execute("SELECT pg_try_advisory_lock(424242)")
        assert cur.fetchone()[0]
        cur.execute("SELECT pg_advisory_unlock(424242)")


def test_rows_come_back_as_postgresql_wrote_them(sql):
    answer = sql.run_query("SELECT 0.1000000000000000055511151231257827::numeric AS x, "
                           "123456789012345678901234567890 AS big, 1 AS id, 2 AS id")
    assert answer == ('[{"x":0.1000000000000000055511151231257827,'
                      '"big":123456789012345678901234567890,"id":1,"id":2}]')


def test_an_answer_stops_at_its_size_budget(sql):
    answer = sql.run_query("SELECT repeat('x', 1000) AS t FROM generate_series(1, 300)")
    note, rows = answer.split("\n", 1)
    assert len(rows) <= sql.CHAR_BUDGET and 70 <= len(json.loads(rows)) < 80
    assert "all an answer may carry" in note
    note, rows = sql.run_query(f"SELECT repeat('x', {sql.CHAR_BUDGET}) AS t").split("\n", 1)
    assert rows == "[]" and "fewer or narrower columns" in note
    note, rows = sql.run_query("SELECT repeat('x', 30000000) AS t").split("\n", 1)
    assert rows == "[]" and "fewer or narrower columns" in note


def test_an_errors_words_are_cut_to_the_budget(sql):
    """PostgreSQL quotes a value it could not read, whole."""
    with pytest.raises(ValueError) as refused:
        sql.run_query(f"SELECT repeat('9', {sql.CHAR_BUDGET * 3})::integer")
    said = str(refused.value)
    assert said.startswith('ERROR:  value "999') and said.endswith("(cut)")
    assert len(said) <= sql.CHAR_BUDGET + 10


def test_an_answer_of_exactly_the_budget_is_whole(sql):
    width = sql.CHAR_BUDGET - len('[{"t":""}]')
    answer = sql.run_query(f"SELECT repeat('x', {width}) AS t")
    assert len(answer) == sql.CHAR_BUDGET and json.loads(answer) == [{"t": "x" * width}]


def test_no_more_than_the_budget_leaves_the_database(sql):
    """Counted where the rows are made: rows wider than the budget never
    reach the tool's process."""
    with sql._connection().cursor() as cur:
        cur.execute("SELECT count(*), coalesce(sum(length(answer_row)), 0) FROM public.sautium_query("
                    "'SELECT repeat(''x'', 2000000) AS t FROM generate_series(1, 50)', %s, %s)",
                    (sql.ROW_CAP, sql.CHAR_BUDGET))
        assert cur.fetchone() == (1, 0)                 # the note's row alone
    sql._conn.rollback()


def test_a_call_makes_nothing_in_the_database(sql):
    """The query runs inside the schema's function: the tool's session holds
    no object of its own, though this role may create one."""
    assert json.loads(sql.run_query(
        "SELECT count(*) AS n FROM pg_proc WHERE pronamespace = pg_my_temp_schema()")) == [{"n": 0}]


def test_an_unreachable_database_is_said_as_the_tools_error(sql, monkeypatch):
    if sql._conn is not None:
        sql._conn.close()
    monkeypatch.setitem(sql.DB, "host", "127.0.0.1")
    monkeypatch.setitem(sql.DB, "port", "1")
    with pytest.raises(ValueError, match="not reachable"):
        sql.run_query("SELECT 1")
