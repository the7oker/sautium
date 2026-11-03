"""What an agent CLI may use is named on every spawn — the Claude runner's
tool profiles (claude_code_runner.LIBRARY / RESEARCH / TEXT). The CLI runs
for real, the binary of the backend image, against a fake of its model API
on loopback (tests/fake_model_api.py): a test reads what the CLI offered
the model, scripts the model's tool calls and sees which of them ran.

The spawn is the runner's own, the demotion to the image's `agent` account
included, so the files the agent's processes use live in a directory that
account can reach. The user configuration the CLI reads (CLAUDE_CONFIG_DIR)
is the test's, so a test can plant a user's own MCP server and permission
rules beside the call. No request leaves the machine: the fake answers
every one, and the suite runs on an internal network."""

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent.parent / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

import claude_code  # noqa: E402
import claude_code_runner as runner  # noqa: E402
import codex_cli  # noqa: E402
import codex_runner  # noqa: E402
from tests.fake_model_api import (FakeModelApi, anthropic_tool_results,  # noqa: E402
                                  anthropic_tools, codex_hosted_tools, codex_instructions,
                                  codex_tool_outputs, codex_tools)

PROBE = Path(__file__).resolve().parent / "mcp_probe_server.py"


def _in_the_image() -> bool:
    try:
        import pwd
        pwd.getpwnam(runner.AGENT_USER)
    except (ImportError, KeyError):
        return False
    return (os.geteuid() == 0 and runner._resolve_claude_executable() is not None
            and codex_runner._resolve_codex_executable() is not None)


pytestmark = pytest.mark.skipif(
    not _in_the_image(),
    reason="the backend image's run: both agent CLIs and the agent account they run as")


@pytest.fixture
def shared():
    """A directory the agent account's processes can read and write."""
    path = Path(tempfile.mkdtemp(prefix="sautium-agent-tools-"))
    path.chmod(0o777)
    yield path
    shutil.rmtree(path, ignore_errors=True)


@pytest.fixture
def claude(shared, monkeypatch):
    """Every Claude Code spawn pointed at a fake API that plays `script`;
    the user configuration in shared/config. Returns the fake."""
    config = shared / "config"
    config.mkdir()
    config.chmod(0o777)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config))
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "test-token")
    for name in ("DISABLE_TELEMETRY", "DISABLE_AUTOUPDATER", "DISABLE_ERROR_REPORTING",
                 "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"):
        monkeypatch.setenv(name, "1")
    monkeypatch.setattr(claude_code, "auth",
                        claude_code.AgentAuth("Claude Code", claude_code._cli_status))
    apis = []

    def at(script):
        api = FakeModelApi(script).__enter__()
        apis.append(api)
        monkeypatch.setenv("ANTHROPIC_BASE_URL", api.url)
        return api

    yield at
    for api in apis:
        api.__exit__(None, None, None)


@pytest.fixture
def codex(shared, monkeypatch):
    """Every codex spawn pointed at a fake API that plays `script` — the
    runner's own flags plus a provider entry naming the fake; the CLI's
    home in shared/codex. Returns the fake."""
    home = shared / "codex"
    home.mkdir()
    home.chmod(0o777)
    (home / "auth.json").write_text("{}")
    monkeypatch.setenv("CODEX_HOME", str(home))
    monkeypatch.setenv("SAUTIUM_TEST_MODEL_KEY", "test-key")
    monkeypatch.setattr(codex_cli, "auth", claude_code.AgentAuth("Codex", codex_cli._cli_status))
    apis = []
    flags = codex_runner._base_flags

    def at(script):
        api = FakeModelApi(script).__enter__()
        apis.append(api)
        provider = ["-c", 'model_provider="fake"', "-c", 'model_providers.fake.name="fake"',
                    "-c", f'model_providers.fake.base_url="{api.url}/v1"',
                    "-c", 'model_providers.fake.wire_api="responses"',
                    "-c", 'model_providers.fake.env_key="SAUTIUM_TEST_MODEL_KEY"',
                    "-c", "model_providers.fake.requires_openai_auth=false"]
        monkeypatch.setattr(codex_runner, "_base_flags", lambda *a, **k: flags(*a, **k) + provider)
        return api

    yield at
    for api in apis:
        api.__exit__(None, None, None)


def _patch_that_adds(path: Path) -> dict:
    """A scripted call to codex's file tool, through its code-mode host,
    adding `path`."""
    patch = f"*** Begin Patch\n*** Add File: {path}\n+text\n*** End Patch\n"
    return {"custom": "exec",
            "input": f"const r = await tools.apply_patch({json.dumps(patch)});\ntext(JSON.stringify(r));\n"}


def _probes(shared: Path, *names: str) -> dict:
    """An MCP configuration of probe servers, each logging the calls that
    reach it to shared/<name>.log."""
    return {"mcpServers": {name: {"command": sys.executable, "args": [str(PROBE)],
                                  "env": {"PROBE_LOG": str(shared / f"{name}.log")}}
                           for name in names}}


def _calls(shared: Path, name: str) -> list[str]:
    log = shared / f"{name}.log"
    return log.read_text(encoding="utf-8").split() if log.exists() else []


def _results(api) -> str:
    return " ".join(r for body in api.bodies("/messages") for r in anthropic_tool_results(body))


def test_a_library_turn_offers_the_model_its_servers_tools_and_runs_only_those(
        claude, shared, monkeypatch):
    """The user's own settings ask for everything — every permission
    skipped, the shell allowed — and change nothing: no built-in tool
    exists, and a tool of a server outside the assistant's own is refused."""
    (shared / "config" / "settings.json").write_text(json.dumps(
        {"permissions": {"defaultMode": "bypassPermissions", "allow": ["Bash(*)"]}}))
    library = shared / "mcp.json"
    library.write_text(json.dumps(_probes(shared, "postgres", "stray")))
    monkeypatch.setattr(runner, "MCP_CONFIG_PATH", str(library))
    api = claude([
        {"tool": "mcp__stray__ping", "input": {"note": "stray"}},
        {"tool": "mcp__postgres__ping", "input": {"note": "postgres"}},
        {"tool": "Bash", "input": {"command": "true"}},
        {"text": "ok"},
    ])

    done = list(runner.call_claude_code_stream("go", "system"))[-1]

    assert done.error is None
    first = api.bodies("/messages")[0]
    assert sorted(anthropic_tools(first)) == ["mcp__postgres__ping", "mcp__stray__ping"]
    assert _calls(shared, "postgres") == ["postgres"]
    assert _calls(shared, "stray") == []
    assert "No such tool available: Bash" in _results(api)


def test_a_resumed_turn_sends_the_system_prompt_its_own_launch_passes(claude, shared, monkeypatch):
    """The player context rides the system prompt, and a CLI that records
    the first one (2.1.312 and later) would resend the first turn's on every
    resume."""
    library = shared / "mcp.json"
    library.write_text(json.dumps({"mcpServers": {}}))
    monkeypatch.setattr(runner, "MCP_CONFIG_PATH", str(library))
    claude([{"text": "first"}])
    first = list(runner.call_claude_code_stream("go", "Now playing: Alpha Song"))[-1]
    assert first.error is None and first.agent_session_id
    api = claude([{"text": "second"}])

    done = list(runner.call_claude_code_stream(
        "and now?", "Now playing: Beta Song", session_id=first.agent_session_id, resume=True))[-1]

    assert done.error is None
    system = json.dumps(api.bodies("/messages")[-1].get("system"))
    assert "Beta Song" in system and "Alpha Song" not in system


_FAKE_CLI = """#!/bin/sh
here=$(dirname "$0")
case "$1" in --help) cat "$here/help.txt"; exit 0;; esac
printf '%s\\0' "$@" > "$here/argv.txt"
echo '{"type":"result","result":"ok","session_id":"s1","is_error":false}'
"""


@pytest.mark.parametrize("help_text, switch", [
    ("  --system-prompt <prompt>\n  --system-prompt-snapshot <on|off>\n", ["--system-prompt-snapshot", "off"]),
    ("  --system-prompt <prompt>\n", []),
])
def test_a_call_turns_the_prompt_record_off_only_on_a_cli_that_lists_the_switch(
        shared, monkeypatch, help_text, switch):
    """An older CLI (2.1.241) refuses the switch as an unknown option."""
    cli = shared / "claude"
    cli.write_text(_FAKE_CLI)
    cli.chmod(0o755)
    (shared / "help.txt").write_text(help_text)
    monkeypatch.setattr(runner, "_resolve_claude_executable", lambda: str(cli))

    result = runner.call_claude_code("hi", "system", tools=runner.TEXT)

    assert result["is_error"] is False
    argv = (shared / "argv.txt").read_text().split("\0")
    at = argv.index("--system-prompt") + 2
    assert argv[at:at + len(switch)] == switch
    assert ("--system-prompt-snapshot" in argv) == bool(switch)


def test_gear_research_offers_the_web_pair_and_loads_no_server_of_the_users(claude, shared):
    import gear_research_worker
    (shared / "config" / ".claude.json").write_text(json.dumps(_probes(shared, "personal")))
    api = claude([{"tool": "mcp__personal__ping", "input": {"note": "personal"}},
                  {"text": "{}"}])

    assert gear_research_worker._call_claude_code_research("system", "research") == {}

    assert sorted(anthropic_tools(api.bodies("/messages")[0])) == ["WebFetch", "WebSearch"]
    assert _calls(shared, "personal") == []


def _local_file(shared: Path) -> Path:
    """A file of the node, readable by the agent account."""
    path = shared / "local-note.txt"
    path.write_text("contents of a local file\n")
    return path


def test_a_file_a_gear_name_mentions_stays_on_the_node(claude, shared):
    """The CLI reads a file a prompt names after an "@"; a gear model's name
    is data, so it rides where nothing is read."""
    import gear_research_worker
    note = _local_file(shared)
    api = claude([{"text": "{}"}])

    gear_research_worker._call_claude_code_research(
        "system", gear_research_worker._user_message("Acme", f"DAC-1 @{note}", "dac"))

    sent = json.dumps(api.bodies("/messages"))
    assert f"DAC-1 @{note}" in sent
    assert "contents of a local file" not in sent


def _outputs(api) -> str:
    return " ".join(o for body in api.bodies("/responses") for o in codex_tool_outputs(body))


def _codex_roster(body: dict) -> tuple[dict, list[str]]:
    """Every tool a codex request offers: per namespace, and inside its
    code-mode host. Codex has no allow-list of its own — its tools are the
    CLI's defaults for the model less the features the runner turns off —
    so a test names the whole set, and a tool a CLI update adds fails it.
    (The fake answers codex's model-metadata request with 404: the set is
    the CLI's own default for the model.)"""
    return ({ns: sorted(names) for ns, names in codex_tools(body).items()},
            sorted(codex_hosted_tools(body)))


# What _codex_roster reads, by codex's version: a one-shot (no MCP server)
# and a chat turn (the probe server as `postgres`), measured against the
# fake. A version that is not here fails the tests that start codex, and
# the version the image installs (backend/agents/codex/package.json) must
# be here.
_CODEX_ROSTERS = {
    "0.149.0": {
        "one-shot": ({"functions": ["exec", "request_user_input", "wait"]},
                     ["apply_patch", "update_plan"]),
        "turn": ({"functions": ["exec", "request_user_input", "wait"], "mcp__postgres": ["ping"]},
                 ["apply_patch", "list_mcp_resource_templates", "list_mcp_resources",
                  "read_mcp_resource", "update_plan"]),
    },
    "0.166.0": {
        "one-shot": ({"functions": ["exec", "request_user_input", "wait"]},
                     ["apply_patch", "list_mcp_resource_templates", "list_mcp_resources",
                      "read_mcp_resource"]),
        "turn": ({"functions": ["exec", "request_user_input", "wait"], "mcp__postgres": ["ping"]},
                 ["apply_patch", "list_mcp_resource_templates", "list_mcp_resources",
                  "read_mcp_resource"]),
    },
}


def _codex_rosters() -> dict:
    """_CODEX_ROSTERS' entry for the codex the runner starts."""
    said = subprocess.run([codex_runner._resolve_codex_executable(), "--version"],
                          capture_output=True, text=True, check=True).stdout
    version = said.split()[-1]
    assert version in _CODEX_ROSTERS, f"codex {version}: its tool sets are not measured"
    return _CODEX_ROSTERS[version]


def test_the_codex_the_lockfile_pins_has_its_tool_sets_measured():
    pinned = json.loads((BACKEND / "agents" / "codex" / "package.json").read_text(
        encoding="utf-8"))["dependencies"]["@openai/codex"]
    assert pinned in _CODEX_ROSTERS


def test_a_codex_one_shot_runs_on_its_own_instructions_and_cannot_write_a_file(codex, shared):
    target = shared / "written.txt"
    api = codex([_patch_that_adds(target), {"text": "Evening Set"}])

    answer = codex_runner.call_codex_oneshot("Name it", "You write short titles.")

    assert answer == "Evening Set"
    assert not target.exists()
    assert "writing is blocked by read-only sandbox" in _outputs(api)
    first = api.bodies("/responses")[0]
    assert codex_instructions(first) == "You write short titles."
    assert "You are Codex" not in json.dumps(first)
    assert _codex_roster(first) == _codex_rosters()["one-shot"]


def test_a_codex_turn_runs_the_assistants_tools_inside_its_sandbox(codex, shared, monkeypatch):
    library = shared / "mcp.json"
    library.write_text(json.dumps(_probes(shared, "postgres")))
    monkeypatch.setattr(runner, "MCP_CONFIG_PATH", str(library))
    target = shared / "written.txt"
    api = codex([{"tool": "ping", "namespace": "mcp__postgres", "input": {"note": "postgres"}},
                 _patch_that_adds(target), {"text": "ok"}])

    done = list(codex_runner.call_codex_stream("go"))[-1]

    assert done.error is None
    assert _calls(shared, "postgres") == ["postgres"]
    assert not target.exists()
    assert _codex_roster(api.bodies("/responses")[0]) == _codex_rosters()["turn"]


def test_a_codex_turn_hands_its_servers_the_login_password_off_its_command_line(
        codex, shared, monkeypatch):
    """Other local accounts can read a process's arguments on macOS."""
    servers = _probes(shared, "postgres")
    servers["mcpServers"]["postgres"]["env"].update(
        DB_PASSWORD="the-login-password", PROBE_SEES="DB_PASSWORD",
        PROBE_SEEN=str(shared / "seen.txt"))
    library = shared / "mcp.json"
    library.write_text(json.dumps(servers))
    monkeypatch.setattr(runner, "MCP_CONFIG_PATH", str(library))
    spawned = []
    spawn = codex_runner._spawn_codex
    monkeypatch.setattr(codex_runner, "_spawn_codex", lambda cmd: spawned.append(cmd) or spawn(cmd))
    codex([{"tool": "ping", "namespace": "mcp__postgres", "input": {"note": "postgres"}},
           {"text": "ok"}])

    done = list(codex_runner.call_codex_stream("go"))[-1]

    assert done.error is None
    assert "the-login-password" not in " ".join(spawned[0])
    assert (shared / "seen.txt").read_text() == "the-login-password"


# ─── Work nobody watches: text in, text out ────────────────────────────────

@pytest.fixture
def api_keys(monkeypatch):
    """Both API providers registered, with keys that only the fake accepts."""
    import providers
    from config import settings
    monkeypatch.setattr(settings, "anthropic_api_key", "test-key")
    monkeypatch.setattr(settings, "openai_api_key", "test-key")
    providers.reset()
    yield
    providers.reset()


@pytest.fixture
def db(scratch_dsn, monkeypatch):
    """The connection pool on the module's scratch database; a cursor on it."""
    import db_pool
    import psycopg2
    import psycopg2.pool
    pool = psycopg2.pool.ThreadedConnectionPool(1, 4, dsn=scratch_dsn)
    monkeypatch.setattr(db_pool, "_pool", pool)
    conn = psycopg2.connect(scratch_dsn)
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute("DELETE FROM listening_sessions")
        cur.execute("DELETE FROM user_settings")
        yield cur
    conn.close()
    pool.closeall()


def _archived_mix(cur) -> str:
    """An archived mix awaiting its name: two tracks of one artist."""
    import uuid
    artist, sid = str(uuid.uuid4()), str(uuid.uuid4())
    cur.execute("INSERT INTO artists (id, name) VALUES (%s, %s)", (artist, f"Artist {artist[:8]}"))
    cur.execute("INSERT INTO listening_sessions (id, origin, title, track_count, ended_at) "
                "VALUES (%s, 'mix', 'Mix', 2, now())", (sid,))
    for pos, title in enumerate(("Night Bus", "Harbour Lights")):
        track = str(uuid.uuid4())
        cur.execute("INSERT INTO tracks (id, title) VALUES (%s, %s)", (track, title))
        cur.execute("INSERT INTO track_artists (track_id, artist_id) VALUES (%s, %s)", (track, artist))
        cur.execute("INSERT INTO session_tracks (session_id, position, track_id) "
                    "VALUES (%s, %s, %s)", (sid, pos, track))
    return sid


def _name_the_mix(sid: str) -> None:
    import threading
    from playback import sessions
    sessions._schedule_mix_title(sid)
    for worker in [t for t in threading.enumerate() if t.name == "mix-title-worker"]:
        worker.join(120)


def _mix_title(cur, sid: str) -> str:
    cur.execute("SELECT title FROM listening_sessions WHERE id = %s", (sid,))
    return cur.fetchone()[0]


def test_a_chat_title_offers_the_model_no_tool_and_no_file_of_the_checkout(claude):
    """The checkout's own CLAUDE.md sits above the backend's directory; a CLI
    started there would send it along."""
    from routers import chat
    api = claude([{"text": "Jazz From Tashkent"}])

    title = chat._generate_session_title("Any jazz from Uzbekistan?", "Yes, three albums.",
                                         "claude_code")

    assert title == "Jazz From Tashkent"
    first = api.bodies("/messages")[0]
    assert anthropic_tools(first) == []
    assert "Senior-engineer level only" not in json.dumps(first)


def test_a_file_a_chat_mentions_stays_out_of_its_title_call(claude, shared):
    from routers import chat
    note = _local_file(shared)
    api = claude([{"text": "Notes On Jazz"}])

    assert chat._generate_session_title(f"Read @{note} please", "I cannot read files.",
                                        "claude_code") == "Notes On Jazz"

    sent = json.dumps(api.bodies("/messages"))
    assert f"Read @{note} please" in sent
    assert "contents of a local file" not in sent


def test_an_ai_canon_batch_offers_the_model_no_tool(claude, shared, monkeypatch):
    import providers
    from canon import ai_canon
    library = shared / "mcp.json"
    library.write_text(json.dumps(_probes(shared, "postgres", "assistant")))
    monkeypatch.setattr(runner, "MCP_CONFIG_PATH", str(library))
    api = claude([{"text": '[{"n": 1, "pick": "SKIP", "conf": "low", "why": "no signal"}]'}])

    decisions = ai_canon._ask("Library artists: #1 ...", "sonnet",
                              providers.get_provider("claude_code"))

    assert decisions == [{"n": 1, "pick": "SKIP", "conf": "low", "why": "no signal"}]
    assert anthropic_tools(api.bodies("/messages")[0]) == []


def test_an_ai_canon_request_names_the_folder_below_the_library_root(claude, db, monkeypatch):
    """What reaches the provider, read off the request itself."""
    import uuid
    import providers
    from canon import ai_canon
    from config import settings
    monkeypatch.setattr(settings, "music_host_path", "/srv/library-root")
    artist, album = str(uuid.uuid4()), str(uuid.uuid4())
    db.execute("INSERT INTO artists (id, name) VALUES (%s, %s)", (artist, f"Кино {artist[:8]}"))
    db.execute("INSERT INTO albums (id, title) VALUES (%s, 'Группа крови')", (album,))
    db.execute("INSERT INTO album_artists (album_id, artist_id) VALUES (%s, %s)", (album, artist))
    db.execute("INSERT INTO album_variants (album_id, directory_path) VALUES (%s, %s)",
               (album, "/srv/library-root/Кино/1988 - Группа крови"))
    ctx, cands = ai_canon._context(artist, "Кино")
    api = claude([{"text": "[]"}])

    ai_canon._ask(ai_canon._prompt([{"n": 1, "name": "Кино", "ctx": ctx, "cands": cands}]),
                  "sonnet", providers.get_provider("claude_code"))

    sent = json.dumps(api.bodies("/messages"), ensure_ascii=False)
    assert "folder: Кино/1988 - Группа крови" in sent
    assert "/srv/library-root" not in sent


def test_an_ai_canon_batch_on_an_api_key_offers_the_model_no_tool(claude, api_keys, monkeypatch):
    import providers
    from canon import ai_canon
    api = claude([{"text": "[]"}])
    monkeypatch.setenv("OPENAI_BASE_URL", f"{api.url}/v1")
    providers.reset()

    assert ai_canon._ask("Library artists: #1 ...", "gpt-5.6-terra",
                         providers.get_provider("openai")) == []

    sent = api.bodies("/chat/completions")[0]
    assert "tools" not in sent
    assert [m["role"] for m in sent["messages"]] == ["system", "user"]


def test_a_title_on_an_api_key_offers_the_model_no_tool(claude, api_keys):
    from routers import chat
    api = claude([{"text": "Jazz From Tashkent"}])

    assert chat._generate_session_title("Any jazz from Uzbekistan?", "Yes.",
                                        "anthropic") == "Jazz From Tashkent"

    assert "tools" not in api.bodies("/messages")[0]


def test_a_mix_with_no_provider_picked_keeps_its_name_and_asks_no_model(claude, db):
    """No pick is the wizard's "no AI": the agent CLIs being installed is
    no reason to send the owner's listening anywhere."""
    api = claude([{"text": "Harbour Nights"}])
    sid = _archived_mix(db)

    _name_the_mix(sid)

    assert api.requests == []
    assert _mix_title(db, sid) == "Mix"


def test_a_mix_is_named_by_the_picked_provider_with_no_tool_offered(claude, db, api_keys):
    api = claude([{"text": "Harbour Nights"}])
    db.execute("INSERT INTO user_settings (key, value) VALUES ('ai.provider', '\"anthropic\"')")
    sid = _archived_mix(db)

    _name_the_mix(sid)

    assert _mix_title(db, sid) == "Harbour Nights"
    sent = api.bodies("/messages")[0]
    assert "tools" not in sent
    assert "<tracks>\nArtist" in sent["messages"][0]["content"]


def test_a_docker_library_turn_reads_the_catalogue_and_is_refused_the_settings(
        claude, shared, scratch_dsn, monkeypatch):
    """The Docker node's own MCP configuration (write_docker_mcp_config),
    the tree's SQL server and the assistant's login, end to end."""
    import db_migrate
    import psycopg2
    from config import settings
    conn = psycopg2.connect(scratch_dsn)
    db_migrate._assistant_role(conn)
    conn.close()
    monkeypatch.setattr(settings, "postgres_db", scratch_dsn.rsplit("/", 1)[1])
    path = shared / "mcp-docker.json"
    monkeypatch.setattr(runner, "MCP_CONFIG_PATH", str(path))
    monkeypatch.setattr(runner, "STALE_MCP_CONFIG", shared / "stale.json")
    runner.write_docker_mcp_config()
    # The image mounts mcp/ inside the backend's folder (/app/mcp); the suite
    # runs a checkout, which keeps it beside backend/
    config = json.loads(path.read_text(encoding="utf-8"))
    for server in config["mcpServers"].values():
        server["args"] = [str(BACKEND.parent / "mcp" / Path(server["args"][0]).name)]
    path.write_text(json.dumps(config), encoding="utf-8")
    api = claude([
        {"tool": "mcp__postgres__query",
         "input": {"sql": "SELECT 'artists-read:' || count(*) AS n FROM artists"}},
        {"tool": "mcp__postgres__query", "input": {"sql": "SELECT value FROM user_settings"}},
        {"text": "ok"},
    ])

    done = list(runner.call_claude_code_stream("go", "system"))[-1]

    assert done.error is None
    results = _results(api)
    # A token without quotes: each CLI wraps and escapes an MCP answer its own way
    assert "artists-read:" in results
    assert results.count("permission denied") == 1
    assert "permission denied for table user_settings" in results
