"""Claude Code (subscription CLI) detection, install and sign-in.

Backend mirror of `desktop/utils.py` for the parts that the Web UI
needs: state detection and npm install. The install needs node/npm on
the host, so it only works when the backend runs as a native process
(launcher mode — the launcher exports `P2P_IDENTITY_DIR`, the marker
for native mode). The sign-in does not: it is the CLI's own headless
login driven over pipes (`desktop/agent_login.py`), so Docker, where
the CLI is baked into the image, signs in from the Web UI too.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Optional, Tuple

logger = logging.getLogger(__name__)


def is_launcher_mode() -> bool:
    """True when backend runs as a native process on the host. Native
    mode is the only one where we can shell out to node/npm/claude
    and open a terminal window.

    A container is never native, whatever else resolves — checked
    first, positively, because the path heuristic below lies on the
    master node: its compose mounts the identity at a CONTAINER path
    (/app/data/node_identity) that exists, so the old check said
    "launcher" inside Docker and the signin endpoint tried to open a
    terminal instead of returning the use-the-host guidance (surfaced
    via the codex Reauthorize button, 2026-08-23).

    The launcher writes its identity dir into the .env it passes to
    the Docker backend (so the container can reuse the same Ed25519
    key), so the *presence* of P2P_IDENTITY_DIR isn't enough — under
    launcher-managed Docker the path is a Windows path
    (`C:\\Users\\...\\node_identity`) that doesn't resolve inside the
    Linux container. Require the path to actually exist on this
    filesystem."""
    if Path("/.dockerenv").exists() or Path("/run/.containerenv").exists():
        return False
    from config import settings
    if not settings.p2p_identity_dir:
        return False
    try:
        return Path(settings.p2p_identity_dir).exists()
    except OSError:
        return False


# --- Node / npm -------------------------------------------------------------

def get_node_executable() -> Optional[Path]:
    """Locate `node` on PATH. The launcher's installer drops Node next
    to Sautium on Windows; on macOS/Linux it lives wherever the user's
    package manager put it."""
    node = shutil.which("node")
    return Path(node) if node else None


def get_npm_executable() -> Optional[Path]:
    """`npm.cmd` on Windows, `npm` elsewhere — always next to `node`."""
    npm = shutil.which("npm.cmd") or shutil.which("npm")
    if npm:
        return Path(npm)
    node = get_node_executable()
    if node is None:
        return None
    for name in ("npm.cmd", "npm"):
        cand = node.parent / name
        if cand.is_file():
            return cand
    return None


def detect_node_version() -> Optional[Tuple[int, int, int]]:
    """`node --version` parsed to (major, minor, patch), or None."""
    node = get_node_executable()
    if node is None:
        return None
    try:
        kwargs = {
            "capture_output": True, "text": True, "timeout": 10,
            "encoding": "utf-8", "errors": "replace",
        }
        if sys.platform == "win32":
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
        result = subprocess.run([str(node), "--version"], **kwargs)
    except Exception as e:
        logger.debug(f"node --version failed: {e}")
        return None
    if result.returncode != 0:
        return None
    raw = result.stdout.strip().lstrip("v")
    parts = raw.split(".")
    if len(parts) < 3:
        return None
    try:
        return (int(parts[0]), int(parts[1]), int(parts[2]))
    except ValueError:
        return None


# --- Claude binary location -------------------------------------------------

def get_claude_prefix() -> Path:
    """Per-user prefix where `npm install` places Claude Code so we
    don't pollute the user's global node_modules. Matches
    desktop/utils.py.get_claude_prefix()."""
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
    return base / "Sautium" / "claude-prefix"


def get_claude_executable() -> Optional[Path]:
    """Path to the native Claude Code binary. See desktop/utils.py
    for the rationale about bypassing the npm .cmd shim on Windows."""
    bin_rel = (
        Path("node_modules") / "@anthropic-ai" / "claude-code"
        / "bin" / "claude.exe"
    )
    bundled = get_claude_prefix() / bin_rel
    if bundled.is_file():
        return bundled

    shim = shutil.which("claude")
    if shim:
        shim_dir = Path(shim).parent
        for cand in (
            shim_dir.parent / "@anthropic-ai" / "claude-code" / "bin" / "claude.exe",
            shim_dir / "node_modules" / "@anthropic-ai" / "claude-code" / "bin" / "claude.exe",
        ):
            if cand.is_file():
                return cand
        return Path(shim)
    return None


def claude_authenticated() -> bool:
    """True iff Claude Code has stored OAuth credentials. Storage is
    platform-dependent: macOS Keychain entry "Claude Code-credentials",
    Windows/Linux JSON at ~/.claude/.credentials.json.

    On Linux (incl. Docker) the CLI is launched as AGENT_USER by
    `claude_code_runner._spawn_claude` (the CLI refuses to run as root
    with --dangerously-skip-permissions), so credentials live in
    that user's HOME — not the backend process's HOME, which under
    Docker is /root and finds nothing while the host mount puts the
    file at /home/agent/.claude/.credentials.json."""
    if sys.platform == "darwin":
        try:
            result = subprocess.run(
                ["security", "find-generic-password",
                 "-s", "Claude Code-credentials"],
                capture_output=True, timeout=5,
            )
            return result.returncode == 0
        except Exception as e:
            logger.debug(f"Keychain probe failed: {e}")
            return False

    home = Path.home()
    if sys.platform == "linux":
        try:
            import pwd
            from claude_code_runner import AGENT_USER
            home = Path(pwd.getpwnam(AGENT_USER).pw_dir)
        except (KeyError, ImportError) as e:
            logger.debug(f"AGENT_USER lookup failed, falling back to Path.home(): {e}")

    creds = home / ".claude" / ".credentials.json"
    if not creds.is_file():
        return False
    try:
        json.loads(creds.read_text(encoding="utf-8"))
        return True
    except (json.JSONDecodeError, OSError) as e:
        logger.debug(f"Claude credentials unreadable: {e}")
        return False


# --- State machine ----------------------------------------------------------

def get_state() -> str:
    """Return one of: 'host_unsupported', 'node_missing', 'claude_missing',
    'not_authed', 'ready'.

    Fact-based detection runs first: when the `claude` binary is
    present and credentials are readable, the CLI is ready regardless
    of whether the backend is "launcher mode" or "Docker with host
    volume mount" — both serve identical chat requests. This mirrors
    providers._claude_code_ready(); keep the two in sync.

    Without credentials the CLI's presence decides: a present CLI is
    'not_authed' on every runtime, because the sign-in runs headless
    from here (start_signin). Only the install needs the native host,
    so a container WITHOUT the CLI is 'host_unsupported' and native
    mode walks through node/install."""
    claude = get_claude_executable()
    if claude is not None and claude_authenticated():
        return "ready"
    if not is_launcher_mode():
        return "not_authed" if claude is not None else "host_unsupported"
    node_ver = detect_node_version()
    if node_ver is None or node_ver[0] < 18:
        return "node_missing"
    if claude is None:
        return "claude_missing"
    return "not_authed"


# --- Install ----------------------------------------------------------------

def install_claude_runtime() -> Tuple[bool, str]:
    """`npm install --prefix <claude_prefix> @anthropic-ai/claude-code`.
    Long-running and network-bound — caller must run from a worker
    thread / background task. Caller is responsible for verifying Node
    is present (use `detect_node_version` first)."""
    npm = get_npm_executable()
    if npm is None:
        return False, "Node.js not found. Install Node 18+ first."

    prefix = get_claude_prefix()
    prefix.mkdir(parents=True, exist_ok=True)

    cmd = [
        str(npm), "install",
        "--prefix", str(prefix),
        "--no-audit", "--no-fund",
        "@anthropic-ai/claude-code",
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

    if get_claude_executable() is None:
        return False, "Install reported success but claude binary not found in prefix"

    return True, "Claude Code installed"


# --- Sign in ----------------------------------------------------------------

# The one sign-in per process: `claude auth login` driven over pipes. The
# last driver stays for its snapshot (the error of a failed attempt is
# what the UI shows next to the retry button).
_signin = None


def start_signin(on_change) -> dict:
    """Start `claude auth login` with the chat turns' own spawn setup, so
    the credentials land where they read them (the demoted agent user's
    HOME in Docker). A sign-in already running is returned as is."""
    global _signin
    if _signin is not None and _signin.running:
        return _signin.snapshot()
    from desktop.agent_login import AgentLogin, claude_login_command
    from claude_code_runner import spawn_kwargs
    claude = get_claude_executable()
    if claude is None:
        raise RuntimeError("Claude Code CLI not installed")
    env = os.environ.copy()
    # The login must bind the subscription the chat turns bill, not a
    # stray API key — same drop as the runner's _claude_env().
    env.pop("ANTHROPIC_API_KEY", None)
    _signin = AgentLogin("claude", claude_login_command(claude),
                         spawn_kwargs(env), on_change=on_change)
    return _signin.start()


def signin_snapshot() -> Optional[dict]:
    return _signin.snapshot() if _signin is not None else None


def submit_signin_code(code: str) -> dict:
    if _signin is None:
        raise RuntimeError("No sign-in is in progress")
    _signin.submit_code(code)
    return _signin.snapshot()


def cancel_signin() -> Optional[dict]:
    if _signin is None:
        return None
    _signin.cancel()
    return _signin.snapshot()
