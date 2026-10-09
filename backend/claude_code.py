"""Claude Code (subscription CLI) detection, install and sign-in.

Backend counterpart of `desktop/utils.py` for the parts that the Web UI
needs: locating the CLI, npm install, the sign-in. The install needs
node/npm on the host, so it only works when the backend runs as a native
process (launcher mode — the launcher exports `P2P_IDENTITY_DIR`, the
marker for native mode). The sign-in does not: it is the CLI's own
headless login driven over pipes (`desktop/agent_login.py`), so Docker,
where the CLI is baked into the image, signs in from the Web UI too.

Whether the CLI is signed in is the one verdict this module owns for the
whole process (§ Sign-in state): the assistant, the gear research worker,
the AI canon tier and the settings screen all read it here. The launcher
wizard asks the same CLI status one-shot (`desktop/utils.py`), without a
process-wide verdict to keep.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, List, Optional, Tuple

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


# --- Sign-in state ----------------------------------------------------------
#
# Whether a CLI agent can authenticate is the CLI's to say. A credential store
# is not a working sign-in: when a token refresh fails Claude Code keeps
# .credentials.json with its tokens blanked — on 2026-10-09 the Docker node's
# mounted ~/.claude read as "signed in" by file presence while every gear
# research call was refused in two seconds, retried every 15 minutes and
# shown to nobody — on macOS the store is a Keychain item whose existence says
# nothing about its contents, and `codex login status` calls an auth.json of
# `{}` logged in. So two voices of the CLI decide: its own status command,
# which reads its store wherever it lives, and the calls themselves, which the
# runners report as refused or authenticated.
#
# One verdict per agent per process (`auth` here, `codex_cli.auth`). Readers
# take it as it stands (the notices stream derives on every wake, the chat's
# async handlers read it); only the first read, an explicit look (opening the
# AI settings screen, its Refresh) and a finished sign-in ask the CLI again.
# The calls are evidence; a look applies what the store says, in both
# directions — a sign-in made in a terminal ends a refusal there, and dead
# tokens left in place read "logged in" only until the next call refuses
# again — unless a call or a sign-in answered while the CLI was being asked:
# that answer is newer than the store the look read. A flip is an event
# (on_change): the research drain, the notices channel, the guidance trail and
# the settings streams wake on it — none of them re-tries on a timer.

STATUS_TIMEOUT_SECONDS = 30


class AgentAuth:
    """The sign-in verdict of one CLI agent for the whole process."""

    def __init__(self, name: str, status: Callable[[], Optional[bool]]):
        self.name = name
        self._status = status            # the CLI's status command: True/False/None
        self._verdict: Optional[dict] = None
        self._observed = 0               # calls and sign-ins seen; a look started
                                         # before the latest one is out of date
        self._lock = threading.Lock()
        self._look_lock = threading.Lock()
        self._listeners: List[Callable[[bool], None]] = []

    def on_change(self, listener: Callable[[bool], None]) -> None:
        """`listener(signed_in)` runs on every flip of the verdict, on the
        thread that observed it."""
        self._listeners.append(listener)

    def verdict(self) -> Optional[dict]:
        """The standing verdict — signed_in, refused (a call said so),
        reason (the CLI's words), since (the onset) — or None before the
        first look. Never asks the CLI."""
        return self._verdict

    def signed_in(self, fresh: bool = False) -> bool:
        """Can the CLI authenticate? The standing verdict; the first read
        and a `fresh` look ask the CLI's own status first, one look at a
        time."""
        if self._verdict is not None and not fresh:
            return self._verdict["signed_in"]
        with self._look_lock:
            if self._verdict is not None and not fresh:
                return self._verdict["signed_in"]
            observed = self._observed
            status = self._status()
            with self._lock:
                if self._observed != observed:
                    flipped = False      # a call or a sign-in answered meanwhile
                elif status is None:
                    # No answer: nothing learned. Nothing known either →
                    # no evidence against it; the first call says.
                    flipped = self._verdict is None and self._set(True)
                else:
                    flipped = self._set(status)
                now = self._verdict["signed_in"]
        if flipped:
            self._announce(now)
        return now

    def refused(self, reason: str) -> None:
        """The runner: the CLI refused a call for authentication."""
        with self._lock:
            self._observed += 1
            flipped = self._set(False, refused=True, reason=reason)
        if flipped:
            self._announce(False)

    def authenticated(self) -> None:
        """The runner: a call went through."""
        with self._lock:
            self._observed += 1
            flipped = self._set(True)
        if flipped:
            self._announce(True)

    def signin_finished(self, completed: bool) -> None:
        """A sign-in the backend drove has exited. A completed one is a
        signed-in verdict — the CLI stored a fresh credential; any other
        leaves the store to say what is left (`codex login` deletes
        auth.json before it authorizes anything)."""
        if completed:
            self.authenticated()
        else:
            self.signed_in(fresh=True)

    def _set(self, signed_in: bool, refused: bool = False,
             reason: Optional[str] = None) -> bool:
        """Set the verdict under the lock; True when it flipped, and the
        caller then tells the listeners (outside every lock)."""
        prev = self._verdict
        if prev is not None and prev["signed_in"] == signed_in:
            if not signed_in and (refused or reason):
                # The same episode: its onset stays, a refusal and the
                # newest words join it.
                self._verdict = {**prev, "refused": prev["refused"] or refused,
                                 "reason": reason or prev["reason"]}
            return False
        self._verdict = {"signed_in": signed_in, "refused": refused,
                         "reason": reason,
                         "since": datetime.now(timezone.utc).isoformat()}
        logger.info("%s %s%s", self.name, "signed in" if signed_in else "signed out",
                    f" — {reason}" if reason else "")
        return True

    def _announce(self, signed_in: bool) -> None:
        for listener in list(self._listeners):
            try:
                listener(signed_in)
            except Exception:
                logger.exception(f"{self.name} sign-in listener failed")


def _cli_status() -> Optional[bool]:
    """`claude auth status --json` → loggedIn, spawned like a chat turn so
    it reads the store those turns use (the agent user's HOME in Docker,
    the Keychain on macOS); None when the CLI gives no answer."""
    claude = get_claude_executable()
    if claude is None:
        return None
    from claude_code_runner import spawn_kwargs
    env = os.environ.copy()
    # Judged on the subscription the calls bill, not a stray API key.
    env.pop("ANTHROPIC_API_KEY", None)
    kwargs = spawn_kwargs(env)
    kwargs.update(capture_output=True, text=True, encoding="utf-8",
                  errors="replace", timeout=STATUS_TIMEOUT_SECONDS)
    try:
        out = subprocess.run([str(claude), "auth", "status", "--json"], **kwargs)
    except (OSError, subprocess.TimeoutExpired) as e:
        logger.warning(f"claude auth status did not run: {e}")
        return None
    try:
        return bool(json.loads(out.stdout)["loggedIn"])
    except (ValueError, KeyError, TypeError):
        logger.warning(f"claude auth status gave no answer (rc={out.returncode}): "
                       f"{(out.stdout or out.stderr).strip()[:200]}")
        return None


auth = AgentAuth("Claude Code", _cli_status)


# --- State machine ----------------------------------------------------------

def get_state(fresh: bool = False) -> str:
    """Return one of: 'host_unsupported', 'node_missing', 'claude_missing',
    'not_authed', 'ready'. `fresh` asks the CLI again (an explicit look).

    Fact-based detection runs first: when the `claude` binary is
    present and signed in (`auth`), the CLI is ready regardless
    of whether the backend is "launcher mode" or "Docker with host
    volume mount" — both serve identical chat requests.

    Signed out, the CLI's presence decides: a present CLI is
    'not_authed' on every runtime, because the sign-in runs headless
    from here (start_signin). Only the install needs the native host,
    so a container WITHOUT the CLI is 'host_unsupported' and native
    mode walks through node/install."""
    claude = get_claude_executable()
    if claude is not None and auth.signed_in(fresh):
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
    HOME in Docker). A sign-in already running is returned as is. Its
    exit reaches the verdict (`auth.signin_finished`) before `on_change`
    hears it — while it is still this process's sign-in."""
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

    def changed(snap: dict) -> None:
        if not snap["running"] and _signin is driver:
            auth.signin_finished(snap["completed"])
        on_change(snap)

    driver = _signin = AgentLogin("claude", claude_login_command(claude),
                                  spawn_kwargs(env), on_change=changed)
    return driver.start()


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
