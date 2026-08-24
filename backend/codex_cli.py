"""OpenAI Codex CLI detection, install and sign-in.

Codex mirror of `claude_code.py` — state detection, npm install into a
per-user prefix, and launching the interactive sign-in terminal. Same
mode split: install/signin only work when the backend runs natively on
the host (launcher mode); in Docker the CLI is baked into the image and
auth arrives via the ~/.codex volume mount.

Auth model (differs from Claude Code): the CLI reads ONLY
`$CODEX_HOME/auth.json` — a bare OPENAI_API_KEY env var is ignored
(measured on codex-cli 0.149: requests go out with no Authorization
header at all). Subscription sign-in (`codex login`) and
`codex login --with-api-key` both materialize that file. The runner
auto-creates it from OPENAI_API_KEY when missing, so "authenticated"
here means: auth.json exists OR an API key is available to mint it.
"""

from __future__ import annotations

import json
import logging
import os
import platform
import shutil
import sys
from pathlib import Path
from typing import Optional, Tuple

from claude_code import (
    detect_node_version,
    get_npm_executable,
    is_launcher_mode,
)

logger = logging.getLogger(__name__)


# --- Codex binary location ---------------------------------------------------

def get_codex_prefix() -> Path:
    """Per-user prefix where `npm install` places Codex so we don't
    pollute the user's global node_modules. Sibling of claude-prefix."""
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
    return base / "Sautium" / "codex-prefix"


def _native_binary_in(node_modules: Path) -> Optional[Path]:
    """Native codex binary under a node_modules dir. The JS shim
    `bin/codex.js` spawns a platform binary from a separate platform
    package (`@openai/codex-<os>-<arch>/vendor/<triple>/bin/codex[.exe]`)
    — npm places that package HOISTED next to @openai/codex on local
    `--prefix` installs but NESTED inside it on global installs
    (measured both, 2026-08-23). Callers pass every candidate
    node_modules dir; the rust triple is globbed so target renames
    don't break resolution. Mirror of desktop/utils.py — keep in sync."""
    plat = {"win32": "win32", "darwin": "darwin"}.get(sys.platform, "linux")
    arch = "arm64" if platform.machine().lower() in ("arm64", "aarch64") else "x64"
    exe = "codex.exe" if sys.platform == "win32" else "codex"
    vendor = node_modules / "@openai" / f"codex-{plat}-{arch}" / "vendor"
    if not vendor.is_dir():
        return None
    for cand in sorted(vendor.glob(f"*/bin/{exe}")):
        if cand.is_file():
            return cand
    return None


def get_codex_executable() -> Optional[Path]:
    """Path to the codex CLI. Prefers the native binary (skips the
    node shim hop); a shim is an acceptable fallback because codex —
    unlike claude with its ~13.5k-char --system-prompt — never pushes
    argv anywhere near cmd.exe's 8191-char cap (the assistant prompt travels
    as AGENTS.md on disk). Shim priority: the prefix's own
    node_modules/.bin before whatever is on PATH."""
    prefix_nm = get_codex_prefix() / "node_modules"
    nm_candidates = [
        prefix_nm,                                          # local --prefix: hoisted
        prefix_nm / "@openai" / "codex" / "node_modules",   # nested variant
    ]
    shim_candidates: list = []
    shim_names = ("codex.cmd", "codex") if sys.platform == "win32" else ("codex",)
    for name in shim_names:
        cand = prefix_nm / ".bin" / name
        if cand.is_file():
            shim_candidates.append(cand)

    path_shim = shutil.which("codex.cmd") if sys.platform == "win32" else None
    path_shim = path_shim or shutil.which("codex")
    if path_shim:
        shim_dir = Path(path_shim).parent
        for nm in (
            shim_dir / "node_modules",                 # win global: shim beside node_modules
            shim_dir.parent / "lib" / "node_modules",  # unix global: bin/../lib/node_modules
            shim_dir.parent,                           # shim in node_modules/.bin → parent IS node_modules
        ):
            nm_candidates += [nm, nm / "@openai" / "codex" / "node_modules"]
        shim_candidates.append(Path(path_shim))

    for nm in nm_candidates:
        native = _native_binary_in(nm)
        if native is not None:
            return native
    return shim_candidates[0] if shim_candidates else None


# --- Auth --------------------------------------------------------------------

def codex_home() -> Path:
    """$CODEX_HOME, defaulting to <agent user home>/.codex. On Linux
    (incl. Docker) the CLI runs demoted to AGENT_USER, so the home is
    that user's — not the backend process's (/root under Docker)."""
    env_home = os.environ.get("CODEX_HOME")
    if env_home:
        return Path(env_home)
    home = Path.home()
    if sys.platform == "linux":
        try:
            import pwd
            from claude_code_runner import AGENT_USER
            home = Path(pwd.getpwnam(AGENT_USER).pw_dir)
        except (KeyError, ImportError) as e:
            logger.debug(f"AGENT_USER lookup failed, falling back to Path.home(): {e}")
    return home / ".codex"


def codex_auth_file() -> Path:
    return codex_home() / "auth.json"


def codex_authenticated() -> bool:
    """True iff a chat turn can authenticate: auth.json exists
    (subscription sign-in or a previously stored API key), or an
    OPENAI_API_KEY is available for the runner to mint auth.json from
    on first use."""
    if codex_auth_file().is_file():
        return True
    return bool(os.environ.get("OPENAI_API_KEY") or os.environ.get("CODEX_API_KEY"))


def codex_auth_method() -> Optional[str]:
    """Which credential a chat turn will actually run on:
      'chatgpt' — auth.json from a ChatGPT-subscription sign-in,
      'api_key' — auth.json minted from an API key,
      'env_key' — no auth.json yet; the runner will mint one from
                  OPENAI_API_KEY on first use,
      None      — nothing usable.
    Distinguishing chatgpt vs api_key matters in the UI: both render a
    green "ready", but only one bills the subscription."""
    f = codex_auth_file()
    if f.is_file():
        try:
            mode = json.loads(f.read_text(encoding="utf-8")).get("auth_mode")
        except (OSError, json.JSONDecodeError) as e:
            logger.debug(f"codex auth.json unreadable: {e}")
            mode = None
        return "chatgpt" if mode == "chatgpt" else "api_key"
    if os.environ.get("OPENAI_API_KEY") or os.environ.get("CODEX_API_KEY"):
        return "env_key"
    return None


# --- State machine -----------------------------------------------------------

def get_state() -> str:
    """One of: 'host_unsupported', 'node_missing', 'codex_missing',
    'not_authed', 'ready'. Same fact-based-first ordering as
    claude_code.get_state(); mirrors providers._codex_ready() — keep
    the two in sync."""
    if get_codex_executable() is not None and codex_authenticated():
        return "ready"
    if not is_launcher_mode():
        return "host_unsupported"
    node_ver = detect_node_version()
    if node_ver is None or node_ver[0] < 18:
        return "node_missing"
    if get_codex_executable() is None:
        return "codex_missing"
    return "not_authed"


# --- Install -----------------------------------------------------------------

def install_codex_runtime() -> Tuple[bool, str]:
    """`npm install --prefix <codex-prefix> @openai/codex`. Long-running
    and network-bound — caller must run from a worker thread."""
    import subprocess

    npm = get_npm_executable()
    if npm is None:
        return False, "Node.js not found. Install Node 18+ first."

    prefix = get_codex_prefix()
    prefix.mkdir(parents=True, exist_ok=True)

    cmd = [
        str(npm), "install",
        "--prefix", str(prefix),
        "--no-audit", "--no-fund",
        "@openai/codex",
    ]

    kwargs = {
        "capture_output": True, "text": True,
        "encoding": "utf-8", "errors": "replace",
        "timeout": 600,
    }
    if sys.platform == "win32":
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW

    try:
        result = subprocess.run(cmd, **kwargs)
    except subprocess.TimeoutExpired:
        return False, "Install timed out after 10 minutes. Check internet connection."
    except Exception as e:
        return False, f"Install failed: {e}"

    if result.returncode != 0:
        err = (result.stderr or result.stdout or "").strip()
        return False, err[-600:] if err else f"npm exited with code {result.returncode}"

    if get_codex_executable() is None:
        return False, "Install reported success but codex binary not found in prefix"

    return True, "Codex installed"


# --- Sign in -----------------------------------------------------------------

def launch_signin_terminal() -> None:
    """Open `codex login` in a new console/terminal window — the CLI
    prints an auth URL and finishes the ChatGPT OAuth flow itself.
    Caller polls `codex_authenticated()` to detect completion."""
    import subprocess

    codex = get_codex_executable()
    if codex is None:
        raise RuntimeError("Codex CLI not installed")

    if sys.platform == "win32":
        subprocess.Popen(
            ["cmd.exe", "/c", "start", "Codex Setup",
             "cmd.exe", "/k", str(codex), "login"],
            creationflags=subprocess.CREATE_NEW_CONSOLE,
        )
        return
    if sys.platform == "darwin":
        # Path shell-quoted — the bundled prefix lives under
        # "Application Support" and an unquoted space breaks zsh.
        script = (
            f'tell application "Terminal"\n'
            f'  do script "\'{codex}\' login"\n'
            f'  activate\n'
            f'end tell'
        )
        subprocess.Popen(["osascript", "-e", script])
        return
    raise RuntimeError("Interactive Codex setup not supported on this platform")
