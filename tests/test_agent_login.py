"""The headless CLI sign-in driver (desktop/agent_login.py): what it reads
off the two CLIs' output, and how it runs a process — against a stand-in
script that replays the real CLIs' lines (captured 2026-09-09 from
claude-code 2.1.266 and codex-cli 0.149). The real OAuth flows are verified
by hand on the launcher and the Docker node."""

import sys
import textwrap
import threading
import time

import pytest

from desktop import agent_login
from desktop.agent_login import AgentLogin

CLAUDE_URL = (
    "https://claude.com/cai/oauth/authorize?code=true&client_id=9d1c250a-e61b-44d9-88ed-5944d1962f5e"
    "&response_type=code&redirect_uri=https%3A%2F%2Fplatform.claude.com%2Foauth%2Fcode%2Fcallback"
    "&scope=user%3Ainference&code_challenge=qwRz0Ch38gi0Dv&code_challenge_method=S256&state=G0PZtzu2"
)
CLAUDE_OUTPUT = (
    "Opening browser to sign in…\n"
    f"If the browser didn't open, visit: {CLAUDE_URL}\n"
    "Paste code here if prompted > "
)
CODEX_DEVICE_OUTPUT = textwrap.dedent("""\
    Welcome to Codex [v0.149.0]
    OpenAI's command-line coding agent
    Follow these steps to sign in with ChatGPT using device code authorization:
    1. Open this link in your browser and sign in to your account
       https://auth.openai.com/codex/device
    2. Enter this one-time code (expires in 15 minutes)
       3S7N-O960X
    Continue only if you started this login in Codex. If a website or another person gave you this code, cancel.
""")
CODEX_BROWSER_OUTPUT = textwrap.dedent("""\
    Starting local login server on http://localhost:1455.
    If your browser did not open, navigate to this URL to authenticate:
    https://auth.openai.com/oauth/authorize?response_type=code&client_id=app_EMoamEEZ73f0CkXaXp7hrann&redirect_uri=http%3A%2F%2Flocalhost%3A1455%2Fauth%2Fcallback&state=ajkVh2H5
    On a remote or headless machine? Use `codex login --device-auth` instead.
""")


def test_parse_claude_link_and_prompt():
    parsed = agent_login.parse_output("claude", CLAUDE_OUTPUT)
    assert parsed == {"url": CLAUDE_URL, "code": None, "accepts_code": True}


def test_parse_claude_before_the_prompt_arrives():
    head = CLAUDE_OUTPUT.split("Paste")[0]
    parsed = agent_login.parse_output("claude", head)
    assert parsed["url"] == CLAUDE_URL
    assert parsed["accepts_code"] is False


def test_parse_codex_device_flow():
    parsed = agent_login.parse_output("codex", CODEX_DEVICE_OUTPUT)
    assert parsed == {"url": "https://auth.openai.com/codex/device",
                      "code": "3S7N-O960X", "accepts_code": False}


def test_parse_codex_device_flow_coloured_into_a_pipe():
    # codex 0.153 inside the Docker image, captured through the driver.
    text = ("\nWelcome to Codex [v\x1b[90m0.153.4\x1b[0m]\n"
            "1. Open this link in your browser and sign in to your account\n"
            "   \x1b[94mhttps://auth.openai.com/codex/device\x1b[0m\n\n"
            "2. Enter this one-time code \x1b[90m(expires in 15 minutes)\x1b[0m\n"
            "   \x1b[94m3TUR-VBOJ9\x1b[0m\n\n")
    parsed = agent_login.parse_output("codex", text)
    assert parsed == {"url": "https://auth.openai.com/codex/device",
                      "code": "3TUR-VBOJ9", "accepts_code": False}


def test_parse_codex_browser_flow_skips_the_local_server_line():
    parsed = agent_login.parse_output("codex", CODEX_BROWSER_OUTPUT)
    assert parsed["url"].startswith("https://auth.openai.com/oauth/authorize?")
    assert parsed["code"] is None
    assert parsed["accepts_code"] is False


def test_failure_message_drops_the_shared_prompt_line():
    text = CLAUDE_OUTPUT + "Login failed: Request failed with status code 400\n"
    assert agent_login.failure_message(text) == "Login failed: Request failed with status code 400"
    assert agent_login.failure_message("") == "The sign-in process exited without a message."


def test_login_commands():
    assert agent_login.claude_login_command("/x/claude") == ["/x/claude", "auth", "login"]
    assert agent_login.codex_login_command("/x/codex", device=False) == ["/x/codex", "login"]
    assert agent_login.codex_login_command("/x/codex", device=True) == ["/x/codex", "login", "--device-auth"]


# --- the driver against a stand-in CLI -------------------------------------

FAKE_CLI = textwrap.dedent("""\
    import sys, time
    mode = sys.argv[1]
    if mode == "claude":
        sys.stdout.write("Opening browser to sign in…\\n")
        sys.stdout.write("If the browser didn't open, visit: https://claude.com/cai/oauth/authorize?code=true&state=x\\n")
        sys.stdout.write("Paste code here if prompted > ")
        sys.stdout.flush()
        code = sys.stdin.readline().strip()
        if code == "good#code":
            sys.stdout.write("Login successful\\n")
            sys.exit(0)
        sys.stdout.write("Login failed: Request failed with status code 400\\n")
        sys.exit(1)
    if mode == "codex-device":
        sys.stdout.write("1. Open this link in your browser and sign in to your account\\n")
        sys.stdout.write("   https://auth.openai.com/codex/device\\n")
        sys.stdout.write("2. Enter this one-time code (expires in 15 minutes)\\n")
        sys.stdout.write("   3S7N-O960X\\n")
        sys.stdout.flush()
        time.sleep(60)
        sys.exit(0)
    if mode == "hang":
        time.sleep(60)
""")


@pytest.fixture
def fake_cli(tmp_path):
    script = tmp_path / "fake_cli.py"
    script.write_text(FAKE_CLI, encoding="utf-8")
    return [sys.executable, "-u", str(script)]


class Changes:
    """Collects on_change snapshots and lets a test wait for a state."""

    def __init__(self):
        self.snaps = []
        self._cv = threading.Condition()

    def __call__(self, snap):
        with self._cv:
            self.snaps.append(snap)
            self._cv.notify_all()

    def wait_for(self, pred, timeout=10.0):
        deadline = time.monotonic() + timeout
        with self._cv:
            while True:
                for snap in self.snaps:
                    if pred(snap):
                        return snap
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise AssertionError(f"no snapshot matched; got {self.snaps}")
                self._cv.wait(remaining)


def test_claude_flow_accepts_a_pasted_code(fake_cli):
    changes = Changes()
    login = AgentLogin("claude", fake_cli + ["claude"], {}, on_change=changes)
    snap = login.start()
    assert snap["running"] and snap["url"] is None
    ready = changes.wait_for(lambda s: s["accepts_code"])
    assert ready["url"] == "https://claude.com/cai/oauth/authorize?code=true&state=x"
    login.submit_code(" good#code \n")
    done = changes.wait_for(lambda s: not s["running"])
    assert done["exit_code"] == 0 and done["error"] is None and not done["cancelled"]
    assert login.snapshot()["accepts_code"] is False


def test_pipes_stay_binary_under_a_text_mode_spawn_setup(fake_cli):
    # The assistant runners' spawn kwargs carry text=True (their chat
    # turns); the driver must still get a byte stream with read1.
    changes = Changes()
    login = AgentLogin("claude", fake_cli + ["claude"],
                       {"text": True, "encoding": "utf-8", "errors": "replace"},
                       on_change=changes)
    login.start()
    changes.wait_for(lambda s: s["accepts_code"])
    login.submit_code("good#code")
    done = changes.wait_for(lambda s: not s["running"])
    assert done["exit_code"] == 0 and done["error"] is None


def test_claude_flow_reports_a_rejected_code(fake_cli):
    changes = Changes()
    login = AgentLogin("claude", fake_cli + ["claude"], {}, on_change=changes)
    login.start()
    changes.wait_for(lambda s: s["accepts_code"])
    login.submit_code("bogus")
    done = changes.wait_for(lambda s: not s["running"])
    assert done["exit_code"] == 1
    assert done["error"] == "Login failed: Request failed with status code 400"
    with pytest.raises(RuntimeError):
        login.submit_code("late")


def test_submit_code_needs_a_running_prompt(fake_cli):
    login = AgentLogin("codex", fake_cli + ["codex-device"], {}, flow="device")
    with pytest.raises(RuntimeError):
        login.submit_code("x")
    changes = Changes()
    login = AgentLogin("codex", fake_cli + ["codex-device"], {}, on_change=changes, flow="device")
    login.start()
    seen = changes.wait_for(lambda s: s["code"] is not None)
    assert seen["url"] == "https://auth.openai.com/codex/device"
    assert seen["code"] == "3S7N-O960X"
    with pytest.raises(RuntimeError):
        login.submit_code("x")
    with pytest.raises(ValueError):
        login.submit_code("   ")
    login.cancel()
    done = changes.wait_for(lambda s: not s["running"])
    assert done["cancelled"] and done["error"] is None


def test_start_is_idempotent_while_running_and_restarts_after(fake_cli):
    changes = Changes()
    login = AgentLogin("codex", fake_cli + ["hang"], {}, on_change=changes)
    first = login.start()
    again = login.start()
    assert first["started_at"] == again["started_at"] and again["running"]
    login.cancel()
    changes.wait_for(lambda s: not s["running"])
    second = login.start()
    assert second["running"] and second["started_at"] >= first["started_at"]
    assert second["error"] is None and not second["cancelled"]
    login.cancel()
    changes.wait_for(lambda s: not s["running"] and s["started_at"] == second["started_at"])


def test_deadline_kills_and_reports_a_timeout(fake_cli):
    changes = Changes()
    login = AgentLogin("claude", fake_cli + ["hang"], {}, on_change=changes, deadline=0.5)
    login.start()
    done = changes.wait_for(lambda s: not s["running"], timeout=10.0)
    assert done["timed_out"] and "timed out" in done["error"]
