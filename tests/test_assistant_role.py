"""The assistant's login (desktop/db_init.ASSISTANT_ROLE): every SQL path an
agent drives — the MCP servers on both platforms and the API providers'
query tool — connects as a role that reads the catalogue and nothing else,
owns nothing and writes nowhere. The tests make the role and its grants the
way a node does (ensure_assistant_role, then the start's grant step) on the
module's scratch database, and connect as it."""

import json
import sys
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
    "demo_plays", "diag_events", "diag_warrants", "external_api_cooldown",
    "friend_grant_rights", "friend_grants", "friend_rights", "friends",
    "invite_token_rights", "invite_tokens", "lastfm_import", "lb_slice_blobs",
    "lb_slice_fetches", "lb_slice_requests", "mb_slice_blobs", "mb_slice_fetches",
    "p2p_action_costs", "p2p_contact_events", "p2p_dht_state", "p2p_gate_pool",
    "p2p_identities", "p2p_messages", "p2p_node_bans", "p2p_nodes_seen",
    "pending_key_rotations", "pending_scrobble_artists", "pending_scrobbles", "sent_invites",
    "signing_batches", "stream_providers", "support_bundles", "support_reports",
    "support_warrants", "user_profile", "user_settings",
}


@pytest.fixture(scope="module")
def owner(scratch_dsn):
    """The scratch database as a node's start leaves it: migrated, the
    assistant's login made, its grants stated. A connection as its owner."""
    import db_migrate
    conn = psycopg2.connect(scratch_dsn)
    db_migrate._assistant_role(conn)
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


def test_the_assistant_sees_the_output_settings_and_no_other(owner, assistant):
    with owner.cursor() as cur:
        cur.execute("""INSERT INTO user_settings (key, value) VALUES
                       ('output.type', '"hqplayer"'), ('ai.api_key', '"sk-test"')
                       ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value""")
    with assistant.cursor() as cur:
        cur.execute("SELECT key FROM assistant_settings")
        assert [r[0] for r in cur.fetchall()] == ["output.type"]


def test_a_restore_without_privileges_reads_again_after_the_next_start(owner, scratch_dsn):
    """pg_restore --no-privileges brings the tables back without a grant;
    the backend's start states them again."""
    import db_migrate
    with owner.cursor() as cur:
        cur.execute(f"REVOKE ALL ON ALL TABLES IN SCHEMA public FROM {db_init.ASSISTANT_ROLE}")
    conn = psycopg2.connect(scratch_dsn)
    try:
        db_migrate._assistant_role(conn)
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
