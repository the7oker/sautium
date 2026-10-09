"""A CLI agent's sign-in is the CLI's verdict, held once per agent
(backend/claude_code.py AgentAuth — `claude_code.auth`, `codex_cli.auth`):
the CLI's own status command reads its store — the file on Linux and
Windows, the Keychain on macOS, auth.json for Codex — and every call reports
itself refused or authenticated; a refusal outranks a store that still reads
"logged in", and a flip is an event the research drain, the notices channel,
the guidance trail and the AI screen wake on. On 2026-10-09 a credentials
file whose tokens Claude Code had blanked read as "signed in" by its
presence, gear research was refused every 15 minutes with nothing shown to
anyone, and the log said "unknown error" while the cause sat in stdout.

The CLIs are stand-in scripts answering in the shapes the real ones print:
`claude auth status`/`auth login`/`-p` as claude-code 2.1.278–2.1.295
(Windows, macOS and the Docker image), `codex login status`/`login`/`exec
--json` as codex-cli 0.162 (status exit 0/1 with the words on stderr). The
database tests run on the module's scratch database (conftest.scratch_dsn),
the connection pool pointed at it.
"""

import select
import stat
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent.parent / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

import claude_code  # noqa: E402
import claude_code_runner as runner  # noqa: E402
import codex_cli  # noqa: E402
import codex_runner  # noqa: E402

REFUSAL = "Failed to authenticate: OAuth session expired and could not be refreshed"
CODEX_REFUSAL = ("unexpected status 401 Unauthorized: Your access token could not be "
                 "refreshed because your refresh token was already used. Please log "
                 "out and sign in again.")

FAKE_CLAUDE_BODY = r'''
import json, os, sys

args = sys.argv[1:]
with open(os.environ["FAKE_CLI_LOG"], "a", encoding="utf-8") as log:
    log.write(" ".join(args[:2]) + "\n")

if args[:2] == ["auth", "status"]:
    logged_in = os.environ["FAKE_LOGGED_IN"] == "1"
    print(json.dumps({"loggedIn": logged_in,
                      "authMethod": "claude.ai" if logged_in else "none",
                      "apiProvider": "firstParty"}, indent=2))
    sys.exit(0 if logged_in else 1)

if args[:2] == ["auth", "login"]:
    print("Opening browser to sign in...")
    print("If the browser didn't open, visit: https://claude.ai/oauth/authorize?code=true")
    print("Login successful.")
    sys.exit(0)

result = {
    "refused": {"is_error": True, "api_error_status": None,
                "result": "Failed to authenticate: OAuth session expired and could not be refreshed"},
    "not_logged_in": {"is_error": True, "api_error_status": None,
                      "result": "Not logged in · Please run /login"},
    "limit": {"is_error": True, "api_error_status": 429,
              "result": "Claude AI usage limit reached|1760000000"},
    "max_turns": {"is_error": True, "subtype": "error_max_turns"},
    "ok": {"is_error": False, "result": "ok"},
}[os.environ["FAKE_CALL"]]
result.setdefault("subtype", "success")
result.update(type="result", session_id="s-1")
if args[args.index("--output-format") + 1] == "stream-json":
    print(json.dumps({"type": "system", "subtype": "init", "session_id": "s-1",
                      "model": "claude-sonnet", "mcp_servers": [], "tools": []}))
print(json.dumps(result))
sys.exit(1 if result["is_error"] else 0)
'''

FAKE_CODEX_BODY = r'''
import json, os, sys, time

args = sys.argv[1:]
if args[:2] == ["login", "status"]:
    logged_in = os.environ["FAKE_CODEX_LOGGED_IN"] == "1"
    print("Logged in using ChatGPT" if logged_in else "Not logged in", file=sys.stderr)
    sys.exit(0 if logged_in else 1)

if args[:1] == ["login"]:
    # `codex login` deletes auth.json before it authorizes anything.
    auth = os.path.join(os.environ["CODEX_HOME"], "auth.json")
    if os.path.exists(auth):
        os.remove(auth)
    print("Follow these steps to sign in with ChatGPT using device code authorization:")
    print("1. Open this link in your browser and sign in to your account")
    print("   https://auth.openai.com/codex/device")
    print("2. Enter this one-time code (expires in 15 minutes)")
    print("   ABCD-EFGHI", flush=True)
    if os.environ["FAKE_CODEX_LOGIN"] == "wait":
        time.sleep(60)
    with open(auth, "w", encoding="utf-8") as f:
        f.write("{}")
    print("Successfully logged in")
    sys.exit(0)

print(json.dumps({"type": "thread.started", "thread_id": "t-1"}))
if os.environ["FAKE_CODEX_CALL"] == "refused":
    print(json.dumps({"type": "turn.failed", "error": {"message": os.environ["FAKE_CODEX_REFUSAL"]}}))
    sys.exit(1)
print(json.dumps({"type": "item.completed",
                  "item": {"id": "i-1", "type": "agent_message", "text": "ok"}}))
print(json.dumps({"type": "turn.completed", "usage": {}}))
'''


def _script(path: Path, body: str) -> Path:
    path.write_text("#!" + sys.executable + "\n" + body, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


class _Asked:
    """What the stand-in Claude Code was asked, from its own log."""

    def __init__(self, log: Path):
        self.log = log

    def _lines(self):
        return self.log.read_text(encoding="utf-8").splitlines()

    def looks(self) -> int:
        return self._lines().count("auth status")

    def calls(self) -> int:
        return sum(1 for line in self._lines() if line.startswith("-p"))


@pytest.fixture
def cli(tmp_path, monkeypatch):
    """The stand-in Claude Code where every spawn looks for the real one, and
    a process that has not looked at its sign-in yet. Returns what the CLI
    was asked."""
    exe = _script(tmp_path / "claude", FAKE_CLAUDE_BODY)
    log = tmp_path / "calls.log"
    log.touch()
    monkeypatch.setenv("FAKE_CLI_LOG", str(log))
    monkeypatch.setenv("FAKE_LOGGED_IN", "1")
    monkeypatch.setenv("FAKE_CALL", "ok")
    monkeypatch.setattr(claude_code, "get_claude_executable", lambda: exe)
    monkeypatch.setattr(claude_code, "is_launcher_mode", lambda: False)
    monkeypatch.setattr(runner, "_resolve_claude_executable", lambda: str(exe))
    # The demotion to the container's `agent` account is the one part of the
    # spawn a test process cannot take; everything else is the runner's own.
    monkeypatch.setattr(runner, "spawn_kwargs", lambda env: {"env": env})
    monkeypatch.setattr(claude_code, "auth",
                        claude_code.AgentAuth("Claude Code", claude_code._cli_status))
    monkeypatch.setattr(claude_code, "_signin", None)
    return _Asked(log)


@pytest.fixture
def codex(tmp_path, monkeypatch):
    """The stand-in Codex, its own CODEX_HOME holding an auth.json, and a
    process that has not looked at its sign-in yet. Returns the auth.json."""
    exe = _script(tmp_path / "codex", FAKE_CODEX_BODY)
    home = tmp_path / "codex-home"
    home.mkdir()
    auth_json = home / "auth.json"
    auth_json.write_text('{"auth_mode": "chatgpt"}', encoding="utf-8")
    workdir = tmp_path / "codex-agent"
    workdir.mkdir()
    for key in ("OPENAI_API_KEY", "CODEX_API_KEY"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("CODEX_HOME", str(home))
    monkeypatch.setenv("FAKE_CODEX_LOGGED_IN", "1")
    monkeypatch.setenv("FAKE_CODEX_CALL", "ok")
    monkeypatch.setenv("FAKE_CODEX_LOGIN", "ok")
    monkeypatch.setenv("FAKE_CODEX_REFUSAL", CODEX_REFUSAL)
    monkeypatch.setattr(codex_cli, "get_codex_executable", lambda: exe)
    monkeypatch.setattr(codex_cli, "is_launcher_mode", lambda: False)
    # As for Claude: everything but the demotion, and a workdir of the test's.
    monkeypatch.setattr(codex_runner, "_spawn_kwargs", lambda env: {
        "cwd": str(workdir), "stdin": subprocess.DEVNULL, "env": env,
        "text": True, "encoding": "utf-8", "errors": "replace"})
    monkeypatch.setattr(codex_runner, "_write_instructions",
                        lambda wd: wd / codex_runner.INSTRUCTIONS_FILE)
    monkeypatch.setattr(codex_runner, "_codex_workdir", lambda: workdir)
    monkeypatch.setattr(codex_cli, "auth", claude_code.AgentAuth("Codex", codex_cli._cli_status))
    monkeypatch.setattr(codex_cli, "_signin", None)
    return auth_json


@pytest.fixture
def cur(scratch_dsn, monkeypatch):
    import db_pool
    import psycopg2
    import psycopg2.pool
    pool = psycopg2.pool.ThreadedConnectionPool(1, 2, dsn=scratch_dsn)
    monkeypatch.setattr(db_pool, "_pool", pool)
    conn = psycopg2.connect(scratch_dsn)
    conn.autocommit = True
    with conn.cursor() as c:
        for table in ("gear_pair_notes", "gear_models", "gear_brands", "user_settings"):
            c.execute(f"DELETE FROM {table}")
        yield c
    conn.close()
    pool.closeall()


@pytest.fixture
def heard(scratch_dsn):
    """What the notices channel hears from the backend."""
    import psycopg2
    conn = psycopg2.connect(scratch_dsn)
    conn.autocommit = True
    with conn.cursor() as c:
        c.execute("LISTEN sautium_notices")

    def wakes(timeout=2.0):
        got = []
        while select.select([conn], [], [], timeout)[0]:
            conn.poll()
            got += [n.channel for n in conn.notifies]
            conn.notifies.clear()
            timeout = 0.2                         # drain what follows closely
        return got

    yield wakes
    conn.close()


def _queued_model(cur) -> str:
    brand, model = str(uuid.uuid4()), str(uuid.uuid4())
    cur.execute("INSERT INTO gear_brands (id, name) VALUES (%s, 'Meze')", (brand,))
    cur.execute("INSERT INTO gear_models (id, brand_id, model, category) "
                "VALUES (%s, %s, 'ARTA', 'headphones')", (model, brand))
    return model


def _research_state(cur, model: str) -> str:
    cur.execute("SELECT research_state::text FROM gear_models WHERE id = %s", (model,))
    return cur.fetchone()[0]


def _provider(cur, name: str) -> None:
    cur.execute("INSERT INTO user_settings (key, value) VALUES ('ai.provider', %s::jsonb)",
                (f'"{name}"',))


def _wait_for_exit(start) -> None:
    """Run `start(on_change)` and wait for the sign-in it starts to exit."""
    exited = threading.Event()

    def on_change(snap):
        if not snap["running"]:
            exited.set()

    start(on_change)
    assert exited.wait(30)


# ─── Claude Code ─────────────────────────────────────────────────────────────

def test_the_first_read_asks_the_cli_and_then_the_verdict_stands(cli, monkeypatch):
    monkeypatch.setenv("FAKE_LOGGED_IN", "0")     # the tokens blanked in place
    assert claude_code.auth.verdict() is None
    assert claude_code.get_state() == "not_authed"
    assert not claude_code.auth.signed_in()
    assert cli.looks() == 1                       # readers take the verdict

    monkeypatch.setenv("FAKE_LOGGED_IN", "1")     # a sign-in made in a terminal
    assert not claude_code.auth.signed_in()
    assert claude_code.get_state(fresh=True) == "ready"   # the AI screen's look
    assert cli.looks() == 2


def test_a_refusal_holds_until_a_call_or_a_look_says_otherwise(cli, monkeypatch):
    assert claude_code.auth.signed_in()

    monkeypatch.setenv("FAKE_CALL", "refused")
    res = runner.call_claude_code("hi", "system", mcp=False)
    assert (res["answer"], res["is_error"]) == (runner.OAUTH_EXPIRED_MSG, True)
    v = claude_code.auth.verdict()
    assert (v["signed_in"], v["refused"], v["reason"]) == (False, True, REFUSAL)
    assert not claude_code.auth.signed_in()       # readers take the verdict

    # A sign-in made in a terminal: the AI screen's look sees it.
    assert claude_code.auth.signed_in(fresh=True)

    # Tokens left dead in place read "logged in" only until the next call.
    assert runner.call_claude_code("hi", "system", mcp=False)["answer"] == runner.OAUTH_EXPIRED_MSG
    assert not claude_code.auth.signed_in()
    monkeypatch.setenv("FAKE_CALL", "ok")
    res = runner.call_claude_code("hi", "system", mcp=False)
    assert (res["answer"], res["is_error"]) == ("ok", False)
    assert claude_code.auth.signed_in()


def test_a_look_that_a_call_overtook_does_not_apply():
    """The store a look read is older than a call that answered while the
    CLI was being asked: a sign-in completing under an open AI screen must
    not be undone by the screen's look."""
    asked, release = threading.Event(), threading.Event()

    def status():
        asked.set()
        release.wait(10)
        return False                              # what the store said before

    auth = claude_code.AgentAuth("Test", status)
    auth.authenticated()
    look = threading.Thread(target=auth.signed_in, kwargs={"fresh": True})
    look.start()
    assert asked.wait(10)
    auth.authenticated()                          # the sign-in lands meanwhile
    release.set()
    look.join(10)
    assert auth.signed_in()


def test_not_logged_in_is_a_refusal_too(cli, monkeypatch):
    assert claude_code.auth.signed_in()
    monkeypatch.setenv("FAKE_CALL", "not_logged_in")
    assert runner.call_claude_code("hi", "system", mcp=False)["answer"] == runner.OAUTH_EXPIRED_MSG
    assert claude_code.auth.verdict()["reason"] == "Not logged in \u00b7 Please run /login"


def test_any_other_failure_says_why_and_leaves_the_sign_in_alone(cli, monkeypatch):
    assert claude_code.auth.signed_in()
    monkeypatch.setenv("FAKE_CALL", "limit")
    res = runner.call_claude_code("hi", "system", mcp=False)
    assert res["answer"] == "Claude Code error: Claude AI usage limit reached|1760000000"
    assert res["is_error"]
    monkeypatch.setenv("FAKE_CALL", "max_turns")
    res = runner.call_claude_code("hi", "system", mcp=False)
    assert res["answer"] == "Claude Code error: error_max_turns"   # not "unknown error"
    assert claude_code.auth.signed_in()


def test_the_stream_reports_a_refusal_like_the_one_shot(cli, monkeypatch):
    monkeypatch.setenv("FAKE_CALL", "refused")
    done = list(runner.call_claude_code_stream("hi", "system"))[-1]
    assert done.error == runner.OAUTH_EXPIRED_MSG
    assert claude_code.auth.verdict()["refused"]

    monkeypatch.setenv("FAKE_CALL", "ok")
    done = list(runner.call_claude_code_stream("hi", "system"))[-1]
    assert done.error is None
    assert claude_code.auth.signed_in()


def test_a_completed_sign_in_ends_the_episode_and_listeners_hear_flips_only(cli):
    flips = []
    claude_code.auth.on_change(flips.append)
    claude_code.auth.refused(REFUSAL)
    onset = claude_code.auth.verdict()["since"]
    claude_code.auth.refused("API Error: 401 Invalid authentication credentials")
    assert flips == [False]
    assert claude_code.auth.verdict()["since"] == onset       # one episode
    assert claude_code.auth.verdict()["reason"].startswith("API Error: 401")

    _wait_for_exit(claude_code.start_signin)
    assert flips == [False, True]
    assert claude_code.auth.signed_in()


def test_a_signed_out_agent_stays_registered_and_the_registry_never_asks(cli, monkeypatch):
    """A CLI agent is registered when installed, env flag or not: signed out
    it answers with the sign-in instruction, instead of the chat falling
    through to another provider (and its bill). Building the registry — the
    async chat handlers do — spawns nothing, and happens once."""
    import providers
    from config import settings
    monkeypatch.setattr(settings, "claude_code_enabled", False)
    monkeypatch.setenv("FAKE_LOGGED_IN", "0")
    providers.reset()
    try:
        first = providers.get_provider("claude_code")
        assert first is not None
        providers.available_providers()
        assert providers.get_provider("claude_code") is first
        assert cli.looks() == 0
    finally:
        providers.reset()


def test_the_research_queue_waits_for_the_sign_in_and_wakes_on_it(cli, cur, heard, monkeypatch):
    import gear_research_worker as worker
    model = _queued_model(cur)
    claude_code.auth.on_change(worker._on_signin_change)
    monkeypatch.setattr(worker, "_worker_running", True)
    monkeypatch.setenv("FAKE_CALL", "refused")

    started = time.monotonic()
    worker._drain_queue()             # one refused call, then it parks — no timer
    assert time.monotonic() - started < 60
    assert _research_state(cur, model) == "queued"            # never 'failed'
    assert "sautium_notices" in heard()
    assert not claude_code.auth.signed_in()

    worker._drain_wake.clear()
    worker._drain_queue()             # a wake while signed out claims nothing
    assert _research_state(cur, model) == "queued"
    assert claude_code.auth.verdict()["refused"]

    claude_code.auth.authenticated()                          # a chat turn went through
    assert worker._drain_wake.is_set()


def test_a_node_whose_agent_is_the_api_researches_on_its_key(cli, cur, monkeypatch):
    """The image ships the CLI to every Docker node; an owner without a
    Claude subscription who chose the Anthropic API gets research on that
    key — never a queue waiting for a sign-in that will not come."""
    import gear_research_worker as worker
    from config import settings
    settings_router = pytest.importorskip("routers.settings")
    model = _queued_model(cur)
    _provider(cur, "anthropic")
    monkeypatch.setattr(settings, "anthropic_api_key", "test-key")
    monkeypatch.setenv("FAKE_LOGGED_IN", "0")
    billed = []
    monkeypatch.setattr(worker, "_call_anthropic_research",
                        lambda system, user: billed.append(1) or {"research_summary": "via the API"})
    monkeypatch.setattr(worker, "_worker_running", True)
    monkeypatch.setattr(worker, "PAUSE_BETWEEN_MODELS", 0)

    assert not claude_code.auth.signed_in()
    assert "claude_code.signed_out" not in {n["key"] for n in settings_router._notices_state()["items"]}
    worker._drain_queue()
    assert _research_state(cur, model) == "cached"
    assert (billed, cli.calls()) == ([1], 0)


def test_a_model_that_spends_its_turns_fails_instead_of_holding_the_queue(cli, cur, monkeypatch):
    """A failure that belongs to the model is not infrastructure: requeued
    it would come back first on every pass and hold every model behind it."""
    import gear_research_worker as worker
    model = _queued_model(cur)
    monkeypatch.setenv("FAKE_CALL", "max_turns")
    monkeypatch.setattr(worker, "_call_anthropic_research", lambda system, user: None)
    monkeypatch.setattr(worker, "_worker_running", True)
    monkeypatch.setattr(worker, "PAUSE_BETWEEN_MODELS", 0)

    started = time.monotonic()
    worker._drain_queue()
    assert time.monotonic() - started < 60                    # no 15-minute pause
    assert _research_state(cur, model) == "failed"


def test_the_signed_out_notice_names_what_waits_for_the_sign_in(cli, cur):
    settings_router = pytest.importorskip("routers.settings")

    def notice():
        return next((n for n in settings_router._notices_state()["items"]
                     if n["key"] == "claude_code.signed_out"), None)

    assert notice() is None                       # nothing has looked yet
    claude_code.auth.refused(REFUSAL)
    assert notice() is None                       # and nothing needs it

    _queued_model(cur)
    n = notice()
    assert (n["kind"], n["since"]) == ("error", claude_code.auth.verdict()["since"])
    assert n["data"] == {"waiting": 1, "assistant": False}

    _provider(cur, "claude_code")
    assert notice()["data"] == {"waiting": 1, "assistant": True}

    claude_code.auth.authenticated()
    assert notice() is None


# ─── Codex ───────────────────────────────────────────────────────────────────

def test_codex_reads_its_own_status_and_a_refused_turn_outranks_it(codex, monkeypatch):
    assert codex_cli.get_state() == "ready"       # `codex login status` exit 0

    monkeypatch.setenv("FAKE_CODEX_CALL", "refused")
    done = list(codex_runner.call_codex_stream("hi"))[-1]
    assert done.error == codex_runner.CODEX_LOGIN_MSG
    v = codex_cli.auth.verdict()
    assert (v["signed_in"], v["refused"], v["reason"]) == (False, True, CODEX_REFUSAL)
    assert codex_cli.get_state() == "not_authed"
    # The AI screen's look takes the store's word — it reads presence (0.162
    # calls `{}` logged in), so the next turn is what tells a dead file.
    assert codex_cli.get_state(fresh=True) == "ready"
    done = list(codex_runner.call_codex_stream("hi"))[-1]
    assert codex_cli.get_state() == "not_authed"

    monkeypatch.setenv("FAKE_CODEX_CALL", "ok")
    done = list(codex_runner.call_codex_stream("hi"))[-1]
    assert done.error is None
    assert codex_cli.get_state() == "ready"


def test_only_words_of_a_refused_account_count_as_a_codex_refusal():
    for refusal in (CODEX_REFUSAL, "Not logged in", "Incorrect API key provided: sk-***"):
        assert codex_runner._auth_error(refusal), refusal
    for other in ("Rate limit reached for gpt-5.6: Limit 30000, Used 28600, Requested 1401.",
                  "2026-10-09T14:01:23.401Z ERROR codex_core: stream disconnected",
                  "stream error: unexpected status 500 Internal Server Error"):
        assert not codex_runner._auth_error(other), other


def test_codex_without_auth_json_is_signed_in_only_with_a_key_to_mint_from(codex, monkeypatch):
    codex.unlink()
    assert codex_cli.get_state() == "not_authed"
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    assert codex_cli.get_state(fresh=True) == "ready"


def test_a_cancelled_codex_login_reads_as_signed_out_at_once(codex, monkeypatch):
    """`codex login` deletes auth.json first: a sign-in abandoned halfway
    leaves the node signed out, and the verdict says so on its exit."""
    flips = []
    assert codex_cli.auth.signed_in()
    codex_cli.auth.on_change(flips.append)
    monkeypatch.setenv("FAKE_CODEX_LOGIN", "wait")

    exited = threading.Event()

    def on_change(snap):
        if snap["code"] and snap["running"]:
            codex_cli.cancel_signin()
        if not snap["running"]:
            exited.set()

    codex_cli.start_signin(on_change, device=True)
    assert exited.wait(30)
    assert flips == [False]
    assert not codex.exists()

    monkeypatch.setenv("FAKE_CODEX_LOGIN", "ok")
    _wait_for_exit(lambda cb: codex_cli.start_signin(cb, device=True))
    assert flips == [False, True]
    assert codex_cli.auth.signed_in()


def test_a_codex_sign_in_replaced_by_another_flow_leaves_the_verdict_alone(codex, monkeypatch):
    """Switching from the browser flow to a code cancels the first sign-in,
    whose exit says nothing about a store the second is rewriting."""
    monkeypatch.setattr(codex_cli, "is_launcher_mode", lambda: True)
    flips = []
    assert codex_cli.auth.signed_in()
    codex_cli.auth.on_change(flips.append)
    monkeypatch.setenv("FAKE_CODEX_LOGIN", "wait")
    first_shown, first_exited = threading.Event(), threading.Event()

    def first(snap):
        if snap["running"] and snap["url"]:
            first_shown.set()
        if not snap["running"]:
            first_exited.set()

    codex_cli.start_signin(first, device=False)
    assert first_shown.wait(30)
    monkeypatch.setenv("FAKE_CODEX_LOGIN", "ok")
    _wait_for_exit(lambda cb: codex_cli.start_signin(cb, device=True))
    assert first_exited.wait(30)
    assert flips == []                            # never "signed out" halfway
    assert codex_cli.auth.signed_in()


def test_the_codex_notice_is_raised_only_while_the_assistant_runs_on_it(codex, cur):
    settings_router = pytest.importorskip("routers.settings")

    def keys():
        return {n["key"] for n in settings_router._notices_state()["items"]}

    codex_cli.auth.refused(CODEX_REFUSAL)
    assert "codex.signed_out" not in keys()
    _provider(cur, "codex")
    assert "codex.signed_out" in keys()
    codex_cli.auth.authenticated()
    assert "codex.signed_out" not in keys()


def test_the_launcher_wizard_asks_the_same_clis(cli, codex, monkeypatch):
    """desktop/utils — the wizard's one-shot check — reads what the backend's
    verdict reads: the CLIs' own status, not a store that merely exists."""
    utils = pytest.importorskip("desktop.utils")
    monkeypatch.setattr(utils, "get_claude_executable", lambda: cli.log.parent / "claude")
    monkeypatch.setattr(utils, "get_codex_executable", lambda: codex.parent.parent / "codex")
    monkeypatch.setenv("FAKE_LOGGED_IN", "0")
    assert not utils.claude_authenticated()
    monkeypatch.setenv("FAKE_LOGGED_IN", "1")
    assert utils.claude_authenticated()
    assert utils.codex_authenticated()
    monkeypatch.setenv("FAKE_CODEX_LOGGED_IN", "0")
    assert not utils.codex_authenticated()


# ─── the guidance trail ─────────────────────────────────────────────────────

def test_the_trail_leads_to_the_sign_in_while_an_agent_the_node_needs_is_out(
        cli, codex, cur, monkeypatch):
    settings_router = pytest.importorskip("routers.settings")
    monkeypatch.chdir(BACKEND)        # main mounts static/ relative to its directory
    import main
    # The analysis step of the trail probes the hardware; it is not this test's.
    monkeypatch.setitem(main._enrich_state, "running", True)

    def trail():
        return settings_router._guidance_state()["tasks"]

    _queued_model(cur)
    claude_code.auth.refused(REFUSAL)
    assert "ai_signin" in trail()                 # research waits for Claude
    claude_code.auth.authenticated()
    assert "ai_signin" not in trail()             # done is a state, not a visit

    _provider(cur, "codex")
    codex_cli.auth.refused(CODEX_REFUSAL)
    assert "ai_signin" in trail()                 # the assistant waits for Codex
    codex_cli.auth.authenticated()
    assert "ai_signin" not in trail()
