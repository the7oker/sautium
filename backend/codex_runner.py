"""
OpenAI Codex CLI subprocess wrapper for AI DJ.

Codex mirror of `claude_code_runner.py`: spawns `codex exec --json` in
headless mode with the same MCP tools (PostgreSQL + assistant) and turns
its JSONL event stream into provider stream events for the chat router.

Differences from the Claude runner, all forced by the CLI surface:
- No `--system-prompt` flag — the static DJ prompt is written to
  AGENTS.md inside a Sautium-owned working dir (`--cd`), and the
  volatile player context is prefixed into the user message each turn
  (AGENTS.md is read at session start, not reliably re-read on resume).
- No `--mcp-config` — MCP servers are injected per-run as dotted `-c`
  overrides translated from the SAME mcp-docker.json / mcp-windows.json
  the Claude runner uses, so both agents share one MCP source of truth.
  `--ignore-user-config` keeps the user's own ~/.codex/config.toml (and
  any personal MCP connectors in it) out of the run — the codex analog
  of `--strict-mcp-config`.
- No `--disallowed-tools` — shell cannot be denied outright, so the
  built-in shell rides behind three fences: `--disable shell_tool`
  (feature flag), a read-only OS sandbox when available (probed once per
  process — Landlock in containers is a kernel lottery), and a hard
  prompt-level prohibition in CODEX_DJ_SYSTEM_PROMPT.
- Auth is auth.json-only: a bare OPENAI_API_KEY env var is ignored by
  the CLI (measured, 0.149), so when auth.json is missing and a key is
  present the runner mints auth.json via `codex login --with-api-key`
  once. When auth.json exists the key is POPPED from the env — mirror of
  the ANTHROPIC_API_KEY strip: billing must not silently migrate from
  the ChatGPT subscription to the pay-as-you-go API account.
"""

import functools
import json
import logging
import os
import subprocess
import sys
import threading
from pathlib import Path
from typing import Iterator, List, Optional

if sys.platform not in ("win32", "darwin"):
    import pwd

from providers.base import StreamDone, StreamEvent, TextDelta, ToolStart
from claude_code_runner import AGENT_USER, MCP_CONFIG_PATH

logger = logging.getLogger(__name__)

# Slugs from `codex debug models` (2026-08): terra = balanced everyday,
# luna = fast/affordable. Terra default for the same reason claude
# defaults to sonnet — the 150s wallclock leaves no room for
# frontier-model deliberation on a library-search chat turn; the
# frontier tier (sol) is excluded outright, mirroring claude's
# sonnet/haiku-only list.
DEFAULT_MODEL = "gpt-5.6-terra"
ALLOWED_MODELS = {"gpt-5.6-terra", "gpt-5.6-luna"}
TITLE_MODEL = "gpt-5.6-luna"
# Kept below the ~180s at which the browser/LAN drops the streaming
# connection — same rationale as the Claude runner.
TIMEOUT_SECONDS = 150
# Analog of MAX_THINKING_TOKENS=1024: DJ turns are SQL + list-building,
# reasoning depth buys latency, not quality.
REASONING_EFFORT = "low"

CODEX_LOGIN_MSG = (
    "Codex sign-in expired or missing. Run `codex login` in a terminal "
    "on the host machine — the new credentials are picked up "
    "automatically (no restart needed)."
)


def _auth_error(text: Optional[str]) -> bool:
    t = (text or "").lower()
    return (
        "401" in t or "unauthoriz" in t or "not logged in" in t
        or "login" in t or "authenticat" in t
    )


def _codex_home() -> Path:
    from codex_cli import codex_home
    return codex_home()


def _codex_env() -> dict:
    """Env for the codex subprocess. When auth.json exists (subscription
    sign-in or a stored key) the env API keys are dropped so the CLI
    cannot silently bill the API account; without auth.json they are
    kept so `_ensure_auth` can mint the file from them."""
    env = os.environ.copy()
    if (_codex_home() / "auth.json").is_file():
        env.pop("OPENAI_API_KEY", None)
        env.pop("CODEX_API_KEY", None)
    return env


def _resolve_codex_executable() -> Optional[str]:
    from codex_cli import get_codex_executable
    exe = get_codex_executable()
    return str(exe) if exe else None


def _spawn_kwargs(env: dict) -> dict:
    """Popen setup shared by every codex spawn — non-root demote on
    Linux/Docker, no console flash on Windows, stdin closed (the CLI
    reads piped stdin as extra prompt context otherwise)."""
    kwargs: dict = {
        "stdin": subprocess.DEVNULL,
        "env": env,
        "text": True,
        "encoding": "utf-8",
        "errors": "replace",
    }
    if sys.platform == "win32":
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    elif sys.platform != "darwin":
        pw = pwd.getpwnam(AGENT_USER)

        def demote():
            os.setgid(pw.pw_gid)
            os.setuid(pw.pw_uid)

        kwargs["preexec_fn"] = demote
        env["HOME"] = pw.pw_dir
    return kwargs


def _ensure_auth(codex_exe: str) -> Optional[str]:
    """Materialize auth.json from OPENAI_API_KEY when it is missing —
    the CLI never reads the env var itself. Returns an error message,
    or None when auth is in place."""
    if (_codex_home() / "auth.json").is_file():
        return None
    key = os.environ.get("OPENAI_API_KEY") or os.environ.get("CODEX_API_KEY")
    if not key:
        return CODEX_LOGIN_MSG
    env = os.environ.copy()
    kwargs = _spawn_kwargs(env)
    kwargs.update(stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                  stderr=subprocess.STDOUT)
    try:
        proc = subprocess.Popen([codex_exe, "login", "--with-api-key"], **kwargs)
        out, _ = proc.communicate(input=key, timeout=30)
        if proc.returncode != 0:
            logger.error(f"codex login --with-api-key failed: {(out or '').strip()[:300]}")
            return f"Codex API-key login failed: {(out or '').strip()[:200]}"
        logger.info("Codex auth.json minted from OPENAI_API_KEY")
        return None
    except Exception as e:
        logger.error(f"codex login --with-api-key error: {e}")
        return f"Codex API-key login failed: {e}"


@functools.lru_cache(maxsize=1)
def _sandbox_args() -> tuple:
    """Sandbox flags for `codex exec`, decided once per process.

    `--sandbox read-only` is the only mechanical containment of
    model-invented shell (codex has no tool deny-list), but its Linux
    implementation is Landlock — present on some kernels/containers and
    absent on others. `codex sandbox <cmd>` exercises exactly that
    machinery, so a 1s probe tells the truth for THIS runtime. Windows
    has no codex sandbox — the dangerous bypass matches the trust level
    claude runs at there (--dangerously-skip-permissions).
    """
    if sys.platform == "win32":
        return ("--dangerously-bypass-approvals-and-sandbox",)
    codex_exe = _resolve_codex_executable()
    if codex_exe:
        try:
            env = os.environ.copy()
            kwargs = _spawn_kwargs(env)
            kwargs.update(stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            proc = subprocess.Popen(
                [codex_exe, "sandbox", "sh", "-c", "true"], **kwargs)
            if proc.wait(timeout=15) == 0:
                return ("--sandbox", "read-only")
        except Exception as e:
            logger.debug(f"codex sandbox probe failed: {e}")
    logger.warning(
        "codex OS sandbox unavailable on this runtime — falling back to "
        "--dangerously-bypass-approvals-and-sandbox (shell containment is "
        "prompt-level + --disable shell_tool only)")
    return ("--dangerously-bypass-approvals-and-sandbox",)


def _codex_workdir() -> Path:
    """Sautium-owned working root for DJ sessions: holds AGENTS.md (the
    system prompt) and nothing else. NOT the repo and NOT the user's
    own projects — `--cd` points here so codex never walks real files."""
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
        wd = base / "Sautium" / "codex-dj"
    elif sys.platform == "darwin":
        wd = Path.home() / "Library" / "Application Support" / "Sautium" / "codex-dj"
    else:
        try:
            wd = Path(pwd.getpwnam(AGENT_USER).pw_dir) / "codex-dj"
        except KeyError:
            wd = Path.home() / "codex-dj"
    wd.mkdir(parents=True, exist_ok=True)
    return wd


def _write_agents_md(workdir: Path) -> None:
    """Atomic refresh of the DJ system prompt. Rewritten on every spawn
    so NEW sessions always read the current prompt text (rule changes
    ship with code updates); atomic rename keeps a concurrent spawn
    from reading a torn file. Deliberately carries NO volatile data —
    codex reads the file at session start only, so anything live
    (player state, library size) travels per-turn in the message or is
    fetched by the model via SQL."""
    from claude_dj_prompt import get_system_prompt
    prompt = get_system_prompt("codex", None)
    tmp = workdir / "AGENTS.md.tmp"
    tmp.write_text(prompt, encoding="utf-8")
    os.chmod(tmp, 0o644)
    tmp.replace(workdir / "AGENTS.md")


def _mcp_config_overrides() -> List[str]:
    """Translate the shared MCP JSON config (mcp-docker.json /
    mcp-windows.json — the launcher regenerates the latter with live
    ports and credentials) into `-c` args for codex. Dotted keys only:
    per-key form is the robust subset of `-c` TOML parsing, and
    json.dumps of a string/list is valid TOML for these values."""
    args: List[str] = []
    try:
        cfg = json.loads(Path(MCP_CONFIG_PATH).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        logger.error(f"MCP config unreadable at {MCP_CONFIG_PATH}: {e}")
        return args
    for name, server in (cfg.get("mcpServers") or {}).items():
        base = f"mcp_servers.{name}"
        command = server.get("command")
        if not command:
            continue
        args += ["-c", f"{base}.command={json.dumps(command)}"]
        args += ["-c", f"{base}.args={json.dumps(server.get('args') or [])}"]
        for k, v in (server.get("env") or {}).items():
            args += ["-c", f"{base}.env.{k}={json.dumps(str(v))}"]
        # The assistant server imports torch-adjacent modules — give it
        # more than the 10s default before codex declares it dead.
        args += ["-c", f"{base}.startup_timeout_sec=30"]
    return args


def _base_flags(model: str, effort: str, with_mcp: bool,
                resume: bool = False) -> List[str]:
    """`codex exec resume` accepts a NARROWER flag set than `codex exec`
    (measured, 0.149): no `--cd` (the session's recorded cwd — our
    workdir — is restored from the rollout) and no `--sandbox <mode>`
    (only the dangerous bypass flag survived the subcommand split; the
    read-only policy must ride the `-c sandbox_mode=…` config key
    instead, which resume does accept)."""
    flags = ["--json"]
    if not resume:
        flags += ["--cd", str(_codex_workdir())]
    flags += [
        "--skip-git-repo-check",
        # Clean room: the user's own config.toml (personal MCP
        # connectors, model overrides) must not leak into DJ turns.
        # Auth and session storage still use CODEX_HOME (documented).
        "--ignore-user-config",
        "--ignore-rules",
        "-m", model,
        "--disable", "shell_tool",
        "-c", f"model_reasoning_effort={json.dumps(effort)}",
        "-c", 'approval_policy="never"',
    ]
    if with_mcp:
        flags += _mcp_config_overrides()
    sandbox = list(_sandbox_args())
    if resume and sandbox[0] == "--sandbox":
        flags += ["-c", f"sandbox_mode={json.dumps(sandbox[1])}"]
    else:
        flags += sandbox
    return flags


def _spawn_codex(cmd: List[str]):
    env = _codex_env()
    kwargs = _spawn_kwargs(env)
    kwargs.update(stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    return subprocess.Popen(cmd, **kwargs)


def _prefix_player_context(message: str, player_context: Optional[str]) -> str:
    """The volatile now-playing/output state rides in the message (the
    Claude runner bakes it into --system-prompt instead; codex has no
    such flag and AGENTS.md is not reliably re-read on resume)."""
    if not player_context:
        return message
    return (
        "[Player state — live app context refreshed for this message, "
        "not written by the user]\n"
        f"{player_context}\n"
        "[End player state]\n\n"
        f"{message}"
    )


def call_codex_stream(
    message: str,
    player_context: Optional[str] = None,
    thread_id: Optional[str] = None,
    resume: bool = False,
    model: Optional[str] = None,
) -> Iterator[StreamEvent]:
    """Spawn `codex exec --json` (or `codex exec resume <id>`) and turn
    its JSONL events into provider stream events.

    Event mapping (codex exec --json, 0.149):
      thread.started            → capture thread_id (the resume handle)
      item.* type=agent_message → TextDelta of the not-yet-emitted text
                                  suffix (works whether the CLI streams
                                  item.updated deltas or lands the whole
                                  text in one item.completed)
      item.started type=mcp_tool_call / command_execution / web_search
                                → ToolStart (mcp__{server}__{tool} keeps
                                  the frontend pip regexes working)
      turn.failed               → authoritative error
      error                     → transient (reconnects) — candidate
                                  error only if the turn never completes
      turn.completed            → success marker
    """
    use_model = model if model in ALLOWED_MODELS else DEFAULT_MODEL

    codex_exe = _resolve_codex_executable()
    if codex_exe is None:
        logger.error("Codex CLI not found")
        yield StreamDone(
            model=use_model, provider="codex",
            error="Codex CLI is not installed. Open Settings to run AI agent setup.",
        )
        return

    auth_err = _ensure_auth(codex_exe)
    if auth_err:
        yield StreamDone(model=use_model, provider="codex", error=auth_err)
        return

    try:
        _write_agents_md(_codex_workdir())
    except OSError as e:
        logger.error(f"Failed to write codex AGENTS.md: {e}")
        yield StreamDone(model=use_model, provider="codex",
                         error=f"Failed to prepare codex workdir: {e}")
        return

    resuming = bool(resume and thread_id)
    cmd = [codex_exe, "exec"]
    if resuming:
        cmd += ["resume", thread_id]
    cmd += _base_flags(use_model, REASONING_EFFORT, with_mcp=True,
                       resume=resuming)
    cmd += ["--", _prefix_player_context(message, player_context)]

    logger.info(
        f"Codex stream call: message={message[:80]!r}, resume={resume}, "
        f"thread={thread_id}, model={use_model}, sandbox={_sandbox_args()}"
    )

    try:
        proc = _spawn_codex(cmd)
    except FileNotFoundError:
        yield StreamDone(model=use_model, provider="codex",
                         error="Codex CLI is not installed")
        return
    except Exception as e:
        logger.error(f"Failed to spawn codex: {e}")
        yield StreamDone(model=use_model, provider="codex", error=str(e))
        return

    new_thread_id: Optional[str] = None
    tool_calls_count = 0
    seen_tool_ids: set[str] = set()
    emitted_len: dict[str, int] = {}
    turn_completed = False
    error_msg: Optional[str] = None
    transient_error: Optional[str] = None

    timed_out = threading.Event()

    def _watchdog():
        if proc.poll() is None:
            timed_out.set()
            logger.warning(
                f"Codex wallclock timeout ({TIMEOUT_SECONDS}s); killing subprocess")
            try:
                proc.kill()
            except Exception:
                pass

    watchdog = threading.Timer(TIMEOUT_SECONDS, _watchdog)
    watchdog.daemon = True
    watchdog.start()

    stderr_chunks: list[str] = []

    def _drain_stderr():
        if proc.stderr is None:
            return
        try:
            for line in proc.stderr:
                stderr_chunks.append(line)
                low = line.lower()
                # No system/init MCP health report exists in codex's
                # JSONL — a dead tool server only surfaces here. Log
                # loudly so a tool-less answer is diagnosable.
                if "mcp" in low and ("error" in low or "fail" in low):
                    logger.error(f"Codex MCP stderr: {line.strip()[:300]}")
        except Exception:
            pass

    stderr_thread = threading.Thread(target=_drain_stderr, daemon=True)
    stderr_thread.start()

    def _text_suffix(item: dict) -> Optional[str]:
        """The portion of an agent_message's text not yet emitted."""
        item_id = item.get("id") or "item"
        text = item.get("text") or ""
        done = emitted_len.get(item_id, 0)
        if len(text) <= done:
            return None
        emitted_len[item_id] = len(text)
        return text[done:]

    def _tool_name(item: dict) -> str:
        itype = item.get("type")
        if itype == "mcp_tool_call":
            server = item.get("server") or ""
            tool = item.get("tool") or item.get("name") or "tool"
            return f"mcp__{server}__{tool}" if server else str(tool)
        if itype == "command_execution":
            return "shell"
        return str(itype or "tool")

    try:
        assert proc.stdout is not None
        for line in proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                evt = json.loads(line)
            except json.JSONDecodeError:
                logger.debug(f"Codex stream: skipping non-JSON line: {line[:120]}")
                continue

            t = evt.get("type")

            if t == "thread.started":
                new_thread_id = evt.get("thread_id") or new_thread_id

            elif t in ("item.started", "item.updated", "item.completed"):
                item = evt.get("item") or {}
                itype = item.get("type")
                if itype == "agent_message":
                    suffix = _text_suffix(item)
                    if suffix:
                        yield TextDelta(text=suffix)
                elif itype in ("mcp_tool_call", "command_execution",
                               "web_search"):
                    item_id = item.get("id") or ""
                    if not item_id or item_id not in seen_tool_ids:
                        if item_id:
                            seen_tool_ids.add(item_id)
                        tool_calls_count += 1
                        yield ToolStart(name=_tool_name(item))
                elif itype == "error":
                    # Transient plumbing (websocket fallback etc.) —
                    # keep as candidate, turn.failed is authoritative.
                    transient_error = item.get("message") or transient_error
                    logger.debug(f"Codex error item: {item.get('message')}")
                # reasoning / file_change / plan_update — ignore.

            elif t == "turn.completed":
                turn_completed = True

            elif t == "turn.failed":
                raw = (evt.get("error") or {}).get("message") or "Codex error"
                error_msg = CODEX_LOGIN_MSG if _auth_error(raw) else raw

            elif t == "error":
                transient_error = evt.get("message") or transient_error

        rc = proc.wait(timeout=TIMEOUT_SECONDS)
        if not turn_completed and not error_msg:
            stderr_thread.join(timeout=2)
            stderr = "".join(stderr_chunks).strip()
            raw = transient_error or stderr or (
                f"Codex exited with code {rc}" if rc != 0 else None)
            if raw:
                error_msg = CODEX_LOGIN_MSG if _auth_error(raw) else raw
        if rc != 0 and not error_msg and not turn_completed:
            error_msg = f"Codex exited with code {rc}"

    except subprocess.TimeoutExpired:
        logger.error("Codex stream wait() timed out")
        error_msg = error_msg or "Codex timed out"
    except GeneratorExit:
        # Consumer abandoned the generator (GC / caller crash) — kill
        # the subprocess so we don't leak a codex instance.
        logger.info("Codex stream generator dropped; killing subprocess")
        raise
    except Exception as e:
        logger.error(f"Codex stream error: {e}", exc_info=True)
        error_msg = error_msg or str(e)
    finally:
        watchdog.cancel()
        if timed_out.is_set() and not error_msg:
            error_msg = f"Codex timed out after {TIMEOUT_SECONDS}s"
        if proc.poll() is None:
            try:
                proc.terminate()
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
            except Exception:
                pass

    yield StreamDone(
        model=use_model, provider="codex",
        tool_calls_count=tool_calls_count,
        agent_session_id=new_thread_id,
        error=error_msg,
    )


def call_codex_oneshot(
    prompt: str,
    model: str = TITLE_MODEL,
    timeout_seconds: int = 25,
) -> Optional[str]:
    """Bare one-shot codex call — no MCP, no DJ workdir (must not inhale
    AGENTS.md), `--ephemeral` so title generations don't litter session
    storage. Returns the final agent message text, or None on any
    failure — callers treat title generation as best-effort."""
    codex_exe = _resolve_codex_executable()
    if codex_exe is None:
        return None
    if _ensure_auth(codex_exe):
        return None

    import tempfile
    cmd = [
        codex_exe, "exec",
        "--json",
        "--ephemeral",
        "--cd", tempfile.gettempdir(),
        "--skip-git-repo-check",
        "--ignore-user-config",
        "--ignore-rules",
        "-m", model,
        "--disable", "shell_tool",
        "-c", 'model_reasoning_effort="low"',
        "-c", 'approval_policy="never"',
        *_sandbox_args(),
        "--", prompt,
    ]
    try:
        env = _codex_env()
        kwargs = _spawn_kwargs(env)
        result = subprocess.run(
            cmd, capture_output=True, timeout=timeout_seconds, **kwargs)
    except subprocess.TimeoutExpired:
        logger.debug("Codex oneshot timed out")
        return None
    except Exception as e:
        logger.debug(f"Codex oneshot failed: {e}")
        return None

    text: Optional[str] = None
    for line in (result.stdout or "").splitlines():
        try:
            evt = json.loads(line)
        except json.JSONDecodeError:
            continue
        if evt.get("type") == "item.completed":
            item = evt.get("item") or {}
            if item.get("type") == "agent_message" and item.get("text"):
                text = item["text"]
    if text is None and result.returncode != 0:
        logger.debug(
            f"Codex oneshot rc={result.returncode}: "
            f"{(result.stderr or '')[:200]}")
    return text
