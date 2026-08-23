"""Claude Code (subscription CLI) detection, install and sign-in.

Backend mirror of `desktop/utils.py` for the parts that the Web UI
needs: state detection, npm install, and launching the interactive
sign-in terminal. Backend can only do these when it runs as a native
process on the host (launcher mode) — Docker has no node/npm and no
host terminal to open. The launcher exports `P2P_IDENTITY_DIR`, which
we use as the marker for native mode.
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

    The launcher-mode branches only fire as guidance when the CLI
    isn't ready: in Docker without credentials we surface
    'host_unsupported' (point user at the Desktop Launcher); in
    native launcher mode we walk through node/install/signin so the
    Web UI can drive setup."""
    if get_claude_executable() is not None and claude_authenticated():
        return "ready"
    if not is_launcher_mode():
        return "host_unsupported"
    node_ver = detect_node_version()
    if node_ver is None or node_ver[0] < 18:
        return "node_missing"
    if get_claude_executable() is None:
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

def launch_signin_terminal() -> None:
    """Open `claude` in a new console/terminal window so the user can
    run `/login` and complete the OAuth flow. Caller polls
    `claude_authenticated()` to detect completion."""
    claude = get_claude_executable()
    if claude is None:
        raise RuntimeError("Claude Code CLI not installed")

    if sys.platform == "win32":
        subprocess.Popen(
            ["cmd.exe", "/c", "start", "Claude Setup",
             "cmd.exe", "/k", str(claude)],
            creationflags=subprocess.CREATE_NEW_CONSOLE,
        )
        return
    if sys.platform == "darwin":
        script = (
            f'tell application "Terminal"\n'
            f'  do script "{claude}"\n'
            f'  activate\n'
            f'end tell'
        )
        subprocess.Popen(["osascript", "-e", script])
        return
    raise RuntimeError("Interactive Claude setup not supported on this platform")
