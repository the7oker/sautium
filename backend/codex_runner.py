"""
OpenAI Codex CLI subprocess wrapper for AI assistant.

Codex mirror of `claude_code_runner.py`: spawns `codex exec --json` in
headless mode with the same MCP tools (PostgreSQL + assistant) and turns
its JSONL event stream into provider stream events for the chat router.

Differences from the Claude runner, all forced by the CLI surface
(every claim below is measured on codex-cli 0.149, not read off docs):
- No `--system-prompt` flag — the assistant prompt is written to a file
  in a Sautium-owned workdir and handed over as `model_instructions_file`,
  which REPLACES codex's built-in coding-agent prompt (the analog of
  Claude Code's `--system-prompt`, which replaces too). Codex re-reads
  that file on every spawn, `exec resume` included, so a prompt change
  reaches sessions already in flight on their next turn. The earlier
  carrier, AGENTS.md, was a user-role message that codex re-evaluates
  against the process cwd on every run: `exec resume` takes no `--cd`,
  the cwd fell back to the backend's own, no AGENTS.md lives there, and
  codex told the model "the previously provided AGENTS.md instructions
  no longer apply" — every turn after the first ran with no prompt at
  all. The volatile player context still prefixes the user message:
  baked into the instructions it would change the prompt prefix every
  turn and defeat prompt caching.
- Every spawn runs with cwd = the workdir (Popen cwd, since `exec
  resume` rejects `--cd`): the environment context codex shows the
  model, and the root its file tool would write under, must never be
  the backend's directory — on the launcher that is the repo.
- MCP tools are DIRECT function tools. Out of the box codex defers every
  MCP tool behind its code-mode `exec` JS host: the model has to grep
  `ALL_TOOLS` by regex before it can call anything (a round trip per
  turn) and never learns about a tool its regex missed.
  `features.code_mode.direct_only_tool_namespaces` names our servers as
  `mcp__<name>` and puts their tools in the roster upfront — the analog
  of the Claude runner's ENABLE_TOOL_SEARCH=false.
- No `--mcp-config` — MCP servers are injected per-run as dotted `-c`
  overrides translated from the SAME mcp-docker.json / mcp-windows.json
  the Claude runner uses, so both agents share one MCP source of truth.
  `--ignore-user-config` keeps the user's own ~/.codex/config.toml (and
  any personal MCP connectors in it) out of the run — the codex analog
  of `--strict-mcp-config`. Profile files (`-p`) and a project
  `.codex/config.toml` do not load under it, which is why everything
  travels as `-c` flags plus one file path.
- No `--disallowed-tools`, and no sandbox either — under any sandbox
  mode `codex exec` auto-denies every MCP tool call (openai/codex#24135),
  so the dangerous bypass is mandatory. The bypass also flips codex's
  default `web_search` from cached to LIVE, hence the explicit
  `web_search="disabled"`. The fences that remain: `--disable shell_tool`
  (verified — the model has no shell), the plugin / connector /
  agent-team / goal / image tools switched off, and the hard
  prompt-level prohibition in CODEX_SYSTEM_PROMPT on the apply_patch
  file tool, which has no off switch.
- Auth is auth.json-only: a bare OPENAI_API_KEY env var is ignored by
  the CLI (measured, 0.149), so when auth.json is missing and a key is
  present the runner mints auth.json via `codex login --with-api-key`
  once. When auth.json exists the key is POPPED from the env — mirror of
  the ANTHROPIC_API_KEY strip: billing must not silently migrate from
  the ChatGPT subscription to the pay-as-you-go API account.
"""

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
# Measured on the launcher's own complaint ("is there modern jazz in
# Uzbekistan?", terra, 2026-08-25): at "low" the model ran the tag SQL and
# then gave up without naming a single candidate from its own knowledge
# (it resolved a festival); at "medium" it named three, checked them in
# one parallel mb_resolve round and tiled the hit — 32s vs 28s. Discovery
# is the step reasoning depth buys; "low" stays for the title one-shot.
REASONING_EFFORT = "medium"
# The assistant system prompt, as codex's base instructions (see module doc).
INSTRUCTIONS_FILE = "instructions.md"
# Codex surface the assistant has no use for, switched off per spawn. Each
# name was measured to remove real roster/prompt noise: plugins — the
# "plugins available but not installed" list (Spotify, Apple Music, ...)
# shown to the model on every session; apps — connector tools of the
# ChatGPT account; multi_agent — the spawn/wait agent tools (terra keeps
# injecting the "you are /root, the primary agent in a team" preamble
# regardless — that one falls to `agents.enabled=false` below);
# tool_suggest — request_plugin_install; goals and image_generation —
# their tools. Names come from `codex features list`; an unknown name is
# a hard CLI error, so check that list before adding one.
_DISABLED_FEATURES = ("plugins", "apps", "multi_agent", "tool_suggest",
                      "goals", "image_generation")

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
    """Popen setup shared by every codex spawn — cwd pinned to the
    Sautium workdir (`exec resume` takes no `--cd`; without this the
    process inherits the backend's cwd, which on the launcher is the
    repo), non-root demote on Linux/Docker, no console flash on Windows,
    stdin closed (the CLI reads piped stdin as extra prompt context
    otherwise)."""
    kwargs: dict = {
        "cwd": str(_codex_workdir()),
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


def _codex_workdir() -> Path:
    """Sautium-owned working root for assistant sessions: holds the
    instructions file (the system prompt) and nothing else. NOT the repo
    and NOT the user's own projects — every spawn's cwd is here so codex
    never walks real files."""
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
        wd = base / "Sautium" / "codex-agent"
    elif sys.platform == "darwin":
        wd = Path.home() / "Library" / "Application Support" / "Sautium" / "codex-agent"
    else:
        try:
            wd = Path(pwd.getpwnam(AGENT_USER).pw_dir) / "codex-agent"
        except KeyError:
            wd = Path.home() / "codex-agent"
    wd.mkdir(parents=True, exist_ok=True)
    return wd


def _write_instructions(workdir: Path) -> Path:
    """Atomic refresh of the assistant system prompt file. Rewritten on
    every spawn and re-read by codex on every spawn, resume included, so
    rule changes ship with code updates and reach sessions already in
    flight; the atomic rename keeps a concurrent spawn from reading a
    torn file. Player state stays out of it (see module doc). A leftover
    AGENTS.md from the earlier carrier goes away: codex would inject it
    as a second, user-role copy of the prompt."""
    from assistant_prompt import get_system_prompt
    path = workdir / INSTRUCTIONS_FILE
    tmp = workdir / (INSTRUCTIONS_FILE + ".tmp")
    tmp.write_text(get_system_prompt("codex", None), encoding="utf-8")
    os.chmod(tmp, 0o644)
    tmp.replace(path)
    (workdir / "AGENTS.md").unlink(missing_ok=True)
    return path


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
    names: List[str] = []
    for name, server in (cfg.get("mcpServers") or {}).items():
        base = f"mcp_servers.{name}"
        command = server.get("command")
        if not command:
            continue
        names.append(name)
        args += ["-c", f"{base}.command={json.dumps(command)}"]
        args += ["-c", f"{base}.args={json.dumps(server.get('args') or [])}"]
        for k, v in (server.get("env") or {}).items():
            args += ["-c", f"{base}.env.{k}={json.dumps(str(v))}"]
        # The assistant server imports torch-adjacent modules — give it
        # more than the 10s default before codex declares it dead.
        args += ["-c", f"{base}.startup_timeout_sec=30"]
        # Long tools (mb_resolve mint + peer slice fetch) legally run for
        # minutes; codex's 60s default would kill them while claude lets
        # them finish. Align with the chat wallclock — the turn watchdog
        # is the real ceiling either way.
        args += ["-c", f"{base}.tool_timeout_sec={TIMEOUT_SECONDS}"]
    if names:
        # Without this every MCP tool sits behind the code-mode `exec`
        # host: the model greps ALL_TOOLS by regex before it can call one
        # and never learns about a tool its regex missed. Direct-only puts
        # them in the roster as ordinary function tools.
        args += ["-c", "features.code_mode.direct_only_tool_namespaces="
                 + json.dumps([f"mcp__{n}" for n in names])]
    return args


def _base_flags(model: str, effort: str, with_mcp: bool,
                instructions: Optional[Path] = None) -> List[str]:
    """Flags shared by `codex exec` and `codex exec resume` — the same
    set once `--cd` is out of the picture (resume rejects it; the cwd
    comes from Popen instead). `instructions` is the assistant prompt
    file that replaces codex's built-in prompt; the title one-shot runs
    without it, on codex's own.

    The dangerous bypass is MANDATORY, not a fallback: under ANY
    sandbox mode `codex exec` auto-denies every MCP tool call ("MCP
    tool call requires approval, but approval policy is never" —
    measured on macOS Seatbelt; upstream openai/codex#24135 confirms no
    non-interactive allow knob exists). The chat IS its MCP tools, so
    sandboxed codex is a chat that cannot answer. What actually fences
    the agent instead: shell_tool disabled (verified — the model
    reports having no shell), web search disabled explicitly (the
    bypass would otherwise default it to LIVE), the surface in
    _DISABLED_FEATURES switched off, and the prompt-level prohibition in
    CODEX_SYSTEM_PROMPT. Known residual: the apply_patch file tool cannot
    be switched off (no feature flag, include_apply_patch_tool=false is
    inert — both measured) and stays reachable behind the prompt fence
    only."""
    flags = [
        "--json",
        "--skip-git-repo-check",
        # Clean room: the user's own config.toml (personal MCP
        # connectors, model overrides) must not leak into assistant turns.
        # Auth and session storage still use CODEX_HOME (documented).
        "--ignore-user-config",
        "--ignore-rules",
        "-m", model,
        "--disable", "shell_tool",
        "-c", f"model_reasoning_effort={json.dumps(effort)}",
        "-c", 'approval_policy="never"',
        "-c", 'web_search="disabled"',
        "-c", "agents.enabled=false",
        # Every spawn is a fresh process; the CLI's update ping is a
        # network round trip the chat turn never benefits from.
        "-c", "check_for_update_on_startup=false",
    ]
    for feature in _DISABLED_FEATURES:
        flags += ["--disable", feature]
    if instructions is not None:
        flags += ["-c", f"model_instructions_file={json.dumps(str(instructions))}"]
    if with_mcp:
        flags += _mcp_config_overrides()
    flags.append("--dangerously-bypass-approvals-and-sandbox")
    return flags


def _spawn_codex(cmd: List[str]):
    env = _codex_env()
    kwargs = _spawn_kwargs(env)
    kwargs.update(stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    return subprocess.Popen(cmd, **kwargs)


def _prefix_player_context(message: str, player_context: Optional[str]) -> str:
    """The volatile now-playing/output state rides in the message (the
    Claude runner bakes it into --system-prompt instead; here it would
    change the instructions prefix every turn and defeat prompt caching)."""
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
        instructions = _write_instructions(_codex_workdir())
    except OSError as e:
        logger.error(f"Failed to write codex instructions file: {e}")
        yield StreamDone(model=use_model, provider="codex",
                         error=f"Failed to prepare codex workdir: {e}")
        return

    resuming = bool(resume and thread_id)
    cmd = [codex_exe, "exec"]
    if resuming:
        cmd += ["resume", thread_id]
    cmd += _base_flags(use_model, REASONING_EFFORT, with_mcp=True,
                       instructions=instructions)
    cmd += ["--", _prefix_player_context(message, player_context)]

    logger.info(
        f"Codex stream call: message={message[:80]!r}, resume={resume}, "
        f"thread={thread_id}, model={use_model}"
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
        if timed_out.is_set():
            # The watchdog's kill is the story; stderr at this point is
            # codex's own log chatter, not a cause.
            error_msg = error_msg or f"Codex timed out after {TIMEOUT_SECONDS}s"
        elif not turn_completed and not error_msg:
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
    """Bare one-shot codex call — no MCP, codex's own built-in prompt (no
    instructions file), `--ephemeral` so title generations don't litter
    session storage. Returns the final agent message text, or None on
    any failure — callers treat title generation as best-effort."""
    codex_exe = _resolve_codex_executable()
    if codex_exe is None:
        return None
    if _ensure_auth(codex_exe):
        return None

    cmd = [codex_exe, "exec", "--ephemeral"]
    cmd += _base_flags(model, "low", with_mcp=False)
    cmd += ["--", prompt]
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
