"""The assistant's login (desktop/db_init.ASSISTANT_ROLE): every SQL path an
agent drives — the MCP servers on both platforms and the API providers'
query tool — connects as a role that reads the catalogue and nothing else,
writes no table and creates nothing, and whose sessions do not outlive the
agents' turns. The tests make the role and its grants the way a node does
(the cluster administrator's step, then the migration runner's grants) on
the module's scratch database, and connect as it."""

import json
import logging
import sys
import threading
import time
from pathlib import Path

import psycopg2
import pytest
from psycopg2 import errors

BACKEND = Path(__file__).resolve().parent.parent / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from desktop import db_init  # noqa: E402
from tests.conftest import PG  # noqa: E402

# Every relation the assistant's login must not read. A table that is in
# neither this set nor db_init.ASSISTANT_READS fails the first test: a new
# table is a decision.
PRIVATE = {
    "_schema_migrations", "analysis_sources", "chat_messages", "chat_sessions", "covers",
    "diag_events", "diag_warrants", "external_api_cooldown",
    "friend_grant_rights", "friend_grants", "friend_rights", "friends",
    "invite_token_rights", "invite_tokens", "lastfm_import", "lb_slice_blobs",
    "lb_slice_fetches", "lb_slice_requests", "mb_slice_blobs", "mb_slice_fetches",
    "p2p_action_costs", "p2p_contact_events", "p2p_dht_state", "p2p_gate_pool",
    "p2p_identities", "p2p_messages", "p2p_node_bans", "p2p_nodes_seen", "p2p_witness",
    "pending_key_rotations", "pending_scrobble_artists", "pending_scrobbles", "sent_invites",
    "signing_batches", "stream_providers", "support_bundles", "support_reports",
    "support_warrants", "user_profile", "user_settings",
}


@pytest.fixture(scope="module")
def owner(scratch_dsn):
    """The scratch database as a node's start leaves it: the cluster
    administrator's step done (Docker's), migrated, the grants stated. A
    connection as its owner (the suite's role is a superuser)."""
    import db_migrate
    conn = psycopg2.connect(scratch_dsn)
    db_migrate._assistant_role(conn)
    db_init.apply_migrations(conn)
    conn.commit()
    conn.autocommit = True
    yield conn
    conn.close()


def _as_assistant(scratch_dsn):
    conn = psycopg2.connect(host=PG["host"], port=PG["port"],
                            dbname=scratch_dsn.rsplit("/", 1)[1], user=db_init.ASSISTANT_ROLE,
                            password=db_init.assistant_password(PG["password"]))
    conn.autocommit = True
    return conn


@pytest.fixture
def assistant(owner, scratch_dsn):
    conn = _as_assistant(scratch_dsn)
    yield conn
    conn.close()


def test_every_table_and_view_is_read_by_the_assistant_or_kept_from_it(owner):
    with owner.cursor() as cur:
        cur.execute("""SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
                        WHERE n.nspname = 'public' AND c.relkind IN ('r', 'v', 'm', 'p')""")
        relations = {r[0] for r in cur.fetchall()}
    readable = set(db_init.ASSISTANT_READS)
    assert not readable & PRIVATE
    assert relations - readable - PRIVATE == set()
    assert readable - relations == set()


def test_the_assistant_reads_the_catalogue(assistant):
    with assistant.cursor() as cur:
        for relation in db_init.ASSISTANT_READS:
            cur.execute(f'SELECT count(*) FROM "{relation}"')


def test_the_assistant_reads_nothing_private(assistant):
    refused = set()
    with assistant.cursor() as cur:
        for relation in sorted(PRIVATE):
            try:
                cur.execute(f'SELECT 1 FROM "{relation}" LIMIT 1')
            except errors.InsufficientPrivilege:
                refused.add(relation)
    assert refused == PRIVATE


def test_the_assistant_writes_nowhere_even_with_its_read_only_default_lifted(assistant):
    with assistant.cursor() as cur:
        with pytest.raises(errors.ReadOnlySqlTransaction):
            cur.execute("INSERT INTO tags (id, name) VALUES (gen_random_uuid(), 'x')")
        cur.execute("SET default_transaction_read_only = off")
        for statement in ("INSERT INTO tags (id, name) VALUES (gen_random_uuid(), 'x')",
                          "UPDATE user_settings SET value = '0'",
                          "DELETE FROM artists",
                          "CREATE TABLE planted (x int)"):
            with pytest.raises(errors.InsufficientPrivilege):
                cur.execute(statement)


def test_the_assistant_creates_nothing_even_with_its_read_only_default_lifted(assistant):
    with assistant.cursor() as cur:
        cur.execute("SET default_transaction_read_only = off")
        for statement in ("CREATE TEMP TABLE scratch (x int)",
                          "CREATE FUNCTION pg_temp.f() RETURNS int LANGUAGE sql AS 'SELECT 1'",
                          "SELECT lo_create(0)",
                          "SELECT lo_from_bytea(0, 'x')",
                          "CREATE SCHEMA planted"):
            with pytest.raises(errors.InsufficientPrivilege):
                cur.execute(statement)


def test_the_settings_view_is_a_security_barrier(owner):
    with owner.cursor() as cur:
        cur.execute("SELECT reloptions FROM pg_class WHERE relname = 'assistant_settings'")
        assert cur.fetchone()[0] == ["security_barrier=true"]


def test_a_start_takes_back_what_a_session_changed_of_its_own_login(owner, scratch_dsn):
    """PostgreSQL lets a role set its own password and session defaults."""
    database = scratch_dsn.rsplit("/", 1)[1]
    session = _as_assistant(scratch_dsn)
    with session.cursor() as cur:
        cur.execute("SET default_transaction_read_only = off")
        cur.execute(f"ALTER ROLE {db_init.ASSISTANT_ROLE} SET statement_timeout = 0")
        cur.execute(f'ALTER ROLE {db_init.ASSISTANT_ROLE} IN DATABASE "{database}" '
                    "SET statement_timeout = 0")
        cur.execute(f"ALTER ROLE {db_init.ASSISTANT_ROLE} PASSWORD 'changed'")
    session.close()

    with owner.cursor() as cur:
        db_init.ensure_assistant_role(cur, PG["password"])
        cur.execute("SELECT setdatabase, setconfig FROM pg_db_role_setting "
                    "WHERE setrole = %s::regrole", (db_init.ASSISTANT_ROLE,))
        assert cur.fetchall() == [(0, ["default_transaction_read_only=on",
                                       "statement_timeout=30s",
                                       "idle_in_transaction_session_timeout=60s"])]
    again = _as_assistant(scratch_dsn)
    with again.cursor() as cur:
        cur.execute("SHOW statement_timeout")
        assert cur.fetchone()[0] == "30s"
    again.close()


def test_without_the_login_the_grants_wait_and_say_why(owner, monkeypatch, caplog):
    """A launcher whose setup failed before the backend started."""
    monkeypatch.setattr(db_init, "ASSISTANT_ROLE", "sautium_assistant_absent")
    caplog.set_level(logging.ERROR, logger=db_init.logger.name)
    with owner.cursor() as cur:
        db_init.grant_assistant_reads(cur)
    assert "sautium_assistant_absent does not exist" in caplog.text


def test_a_relation_the_database_lacks_costs_the_assistant_only_that_relation(
        owner, scratch_dsn, caplog):
    """A node whose schema drifted from 001 (the master's predates the
    unified file) still starts; the assistant reads the rest."""
    caplog.set_level(logging.ERROR, logger=db_init.logger.name)
    with owner.cursor() as cur:
        cur.execute("ALTER TABLE seed_picks RENAME TO seed_picks_aside")
        try:
            db_init.grant_assistant_reads(cur)
        finally:
            cur.execute("ALTER TABLE seed_picks_aside RENAME TO seed_picks")
            db_init.grant_assistant_reads(cur)
    assert "relations this database lacks: seed_picks" in caplog.text
    reader = _as_assistant(scratch_dsn)
    try:
        with reader.cursor() as cur:
            cur.execute("SELECT count(*) FROM artists")
    finally:
        reader.close()


def test_the_assistant_sees_the_output_settings_and_no_other(owner, assistant):
    with owner.cursor() as cur:
        cur.execute("""INSERT INTO user_settings (key, value) VALUES
                       ('output.type', '"hqplayer"'), ('ai.api_key', '"sk-test"')
                       ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value""")
    with assistant.cursor() as cur:
        cur.execute("SELECT key FROM assistant_settings")
        assert [r[0] for r in cur.fetchall()] == ["output.type"]


def test_a_restore_without_privileges_reads_again_after_the_migration_runner(owner, scratch_dsn):
    """pg_restore --no-privileges brings the tables back without a grant;
    the runner a restore and every start run states them again."""
    with owner.cursor() as cur:
        cur.execute(f"REVOKE ALL ON ALL TABLES IN SCHEMA public FROM {db_init.ASSISTANT_ROLE}")
    conn = psycopg2.connect(scratch_dsn)
    try:
        db_init.apply_migrations(conn)
    finally:
        conn.close()
    reader = _as_assistant(scratch_dsn)
    try:
        with reader.cursor() as cur:
            cur.execute("SELECT count(*) FROM artists")
    finally:
        reader.close()


def test_the_listenbrainz_swap_keeps_the_assistants_read(owner, scratch_dsn):
    import lb_dump_load
    with owner.cursor() as cur:
        cur.execute("CREATE UNLOGGED TABLE lb_stage_recording (recording_mbid uuid NOT NULL, "
                    "listen_count bigint NOT NULL, artist_mbids uuid[] NOT NULL)")
        cur.execute("CREATE UNLOGGED TABLE lb_stage_artist (artist_mbid uuid NOT NULL, "
                    "listen_count bigint NOT NULL)")
    lb_dump_load._aggregate(owner, "20260915", lambda update: None)
    reader = _as_assistant(scratch_dsn)
    try:
        with reader.cursor() as cur:
            cur.execute("SELECT count(*) FROM lb_recording")
            cur.execute("SELECT count(*) FROM lb_artist")
    finally:
        reader.close()


def test_the_query_tool_runs_as_the_assistant_and_leaves_nothing_on_its_session(
        owner, scratch_dsn, monkeypatch):
    from config import settings
    from tools import execute_query as tool
    monkeypatch.setattr(settings, "postgres_db", scratch_dsn.rsplit("/", 1)[1])
    monkeypatch.setattr(tool, "_conn", None)

    assert db_init.ASSISTANT_ROLE in tool.execute_query("SELECT current_user AS who")
    assert "permission denied" in tool.execute_query("SELECT value FROM user_settings")
    tool.execute_query("SELECT pg_advisory_lock(424242) AS held")
    with owner.cursor() as cur:
        cur.execute("SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND objid = 424242")
        assert cur.fetchone()[0] == 0
    tool._conn.close()


def test_the_sql_tool_answers_as_the_assistant_and_makes_nothing(owner, scratch_dsn):
    """The agents' SQL tool (mcp/postgres_server.py) runs its query inside
    the schema's function, so a login that may create nothing — not even in
    pg_temp — gets its answers, and is refused what is private."""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "postgres_server", BACKEND.parent / "mcp" / "postgres_server.py")
    tool = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(tool)
    tool.DB.update(host=PG["host"], port=str(PG["port"]), user=db_init.ASSISTANT_ROLE,
                   password=db_init.assistant_password(PG["password"]),
                   dbname=scratch_dsn.rsplit("/", 1)[1])
    try:
        assert json.loads(tool.run_query(
            "SELECT current_user AS who, "
            "has_database_privilege(current_database(), 'TEMPORARY') AS temp")) == [
            {"who": db_init.ASSISTANT_ROLE, "temp": False}]
        assert len(json.loads(tool.run_query("SELECT count(*) AS n FROM artists"))) == 1
        with pytest.raises(ValueError, match="permission denied for table user_settings"):
            tool.run_query("SELECT value FROM user_settings")
    finally:
        if tool._conn is not None:
            tool._conn.close()


def test_the_query_tool_cancels_a_statement_at_its_deadline(owner, scratch_dsn, monkeypatch):
    """The session's own statement_timeout is a setting it may change; the
    deadline is the tool's."""
    from config import settings
    from tools import execute_query as tool
    monkeypatch.setattr(settings, "postgres_db", scratch_dsn.rsplit("/", 1)[1])
    monkeypatch.setattr(tool, "_conn", None)
    monkeypatch.setattr(tool, "_DEADLINE_SECONDS", 1)

    started = time.monotonic()
    out = tool.execute_query("SELECT pg_sleep(6) AS slept")

    assert "canceling statement due to user request" in out
    assert time.monotonic() - started < 4
    assert "(1 rows)" in tool.execute_query("SELECT 1 AS one")
    tool._conn.close()


@pytest.fixture
def node_pool(scratch_dsn, monkeypatch):
    """The backend's connection pool on the scratch database."""
    import db_pool
    import psycopg2.pool
    pool = psycopg2.pool.ThreadedConnectionPool(1, 2, dsn=scratch_dsn)
    monkeypatch.setattr(db_pool, "_pool", pool)
    yield
    pool.closeall()


def test_the_assistants_sessions_end_with_the_last_live_turn(
        owner, scratch_dsn, node_pool, monkeypatch):
    """A statement a turn's server left running ends when the turns are
    over, not when it would; the query tool's own connection stays."""
    import claude_code_runner as runner
    from config import settings
    from tools import execute_query as tool
    monkeypatch.setattr(settings, "postgres_db", scratch_dsn.rsplit("/", 1)[1])
    monkeypatch.setattr(tool, "_conn", None)
    tool.execute_query("SELECT 1 AS one")
    kept = tool.connection_pid()
    left = _as_assistant(scratch_dsn)
    with left.cursor() as cur:
        cur.execute("SELECT pg_backend_pid()")
        pid = cur.fetchone()[0]
    ended = []

    def statement():
        try:
            with left.cursor() as cur:
                cur.execute("SELECT pg_sleep(20)")
        except psycopg2.Error as e:
            ended.append(type(e).__name__)
    running = threading.Thread(target=statement)

    with runner.library_turn():
        running.start()
        with runner.library_turn():
            pass
        with owner.cursor() as cur:
            cur.execute("SELECT count(*) FROM pg_stat_activity WHERE pid = %s", (pid,))
            assert cur.fetchone()[0] == 1
    running.join(10)

    assert not running.is_alive() and ended
    assert "(1 rows)" in tool.execute_query("SELECT 1 AS one")
    assert tool.connection_pid() == kept
    tool._conn.close()


def test_a_statement_left_by_a_stopped_backend_ends_at_the_next_start(owner, scratch_dsn):
    """No turn of this process will end to end it; the migration runner,
    which every start runs before its deltas, does."""
    left = _as_assistant(scratch_dsn)
    ended = []

    def statement():
        try:
            with left.cursor() as cur:
                cur.execute("SELECT pg_sleep(20)")
        except psycopg2.Error as e:
            ended.append(type(e).__name__)
    running = threading.Thread(target=statement)
    running.start()

    conn = psycopg2.connect(scratch_dsn)
    try:
        db_init.apply_migrations(conn)
    finally:
        conn.close()
    running.join(10)

    assert not running.is_alive() and ended


def test_the_launchers_mcp_file_logs_both_servers_in_as_the_assistant(tmp_path, monkeypatch):
    from desktop import config_manager, python_env
    monkeypatch.setattr(python_env, "get_backend_python", lambda: sys.executable)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    path = tmp_path / "backend" / "mcp-windows.json"
    path.parent.mkdir()
    config_manager.generate_mcp_config({"postgres_password": "owner-secret", "ports": {}}, path)

    servers = json.loads(path.read_text())["mcpServers"]
    assert {name: s["env"]["DB_USER"] for name, s in servers.items()} == {
        "postgres": db_init.ASSISTANT_ROLE, "assistant": db_init.ASSISTANT_ROLE}
    assert "owner-secret" not in path.read_text()


def test_the_docker_mcp_configuration_logs_both_servers_in_as_the_assistant(tmp_path, monkeypatch):
    import claude_code_runner as runner
    from config import settings
    monkeypatch.setattr(runner, "MCP_CONFIG_PATH", str(tmp_path / "mcp-docker.json"))
    monkeypatch.setattr(runner, "STALE_MCP_CONFIG", tmp_path / "stale" / "mcp-docker.json")
    monkeypatch.setattr(runner.os, "geteuid", lambda: 1000)
    monkeypatch.setattr(settings, "postgres_password", "owner-secret")

    runner.write_docker_mcp_config()
    config = runner.library_mcp_config()

    servers = config["mcpServers"]
    assert sorted(servers) == sorted(runner.LIBRARY_SERVERS)
    assert {name: s["env"]["DB_USER"] for name, s in servers.items()} == {
        "postgres": db_init.ASSISTANT_ROLE, "assistant": db_init.ASSISTANT_ROLE}
    assert {s["env"]["DB_PASSWORD"] for s in servers.values()} == {
        db_init.assistant_password("owner-secret")}
    assert "owner-secret" not in json.dumps(config)
