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
                                  anthropic_tools, codex_hosted_tools, codex_tool_outputs,
                                  codex_tools)

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


def test_gear_research_offers_the_web_pair_and_loads_no_server_of_the_users(claude, shared):
    import gear_research_worker
    (shared / "config" / ".claude.json").write_text(json.dumps(_probes(shared, "personal")))
    api = claude([{"tool": "mcp__personal__ping", "input": {"note": "personal"}},
                  {"text": "{}"}])

    assert gear_research_worker._call_claude_code_research("system", "research") == {}

    assert sorted(anthropic_tools(api.bodies("/messages")[0])) == ["WebFetch", "WebSearch"]
    assert _calls(shared, "personal") == []


def _outputs(api) -> str:
    return " ".join(o for body in api.bodies("/responses") for o in codex_tool_outputs(body))


def test_a_codex_one_shot_runs_on_its_own_instructions_and_cannot_write_a_file(codex, shared):
    target = shared / "written.txt"
    api = codex([_patch_that_adds(target), {"text": "Evening Set"}])

    answer = codex_runner.call_codex_oneshot("Name it", "You write short titles.")

    assert answer == "Evening Set"
    assert not target.exists()
    assert "writing is blocked by read-only sandbox" in _outputs(api)
    sent = json.dumps(api.bodies("/responses")[0])
    assert "You write short titles." in sent
    assert "You are Codex" not in sent


def test_a_codex_turn_runs_the_assistants_tools_inside_its_sandbox(codex, shared, monkeypatch):
    library = shared / "mcp.json"
    library.write_text(json.dumps(_probes(shared, "postgres")))
    monkeypatch.setattr(codex_runner, "MCP_CONFIG_PATH", str(library))
    target = shared / "written.txt"
    api = codex([{"tool": "ping", "namespace": "mcp__postgres", "input": {"note": "postgres"}},
                 _patch_that_adds(target), {"text": "ok"}])

    done = list(codex_runner.call_codex_stream("go"))[-1]

    assert done.error is None
    assert _calls(shared, "postgres") == ["postgres"]
    assert not target.exists()
    first = api.bodies("/responses")[0]
    assert codex_tools(first)["mcp__postgres"] == ["ping"]
    assert "view_image" not in codex_hosted_tools(first)
