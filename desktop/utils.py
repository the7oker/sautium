"""
Utility functions for Sautium desktop launcher.
"""

import json
import logging
import os
import platform
import shutil
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Optional, Tuple

logger = logging.getLogger(__name__)


def get_project_root() -> Path:
    """Get project root directory, works in both dev and PyInstaller mode."""
    if getattr(sys, "frozen", False):
        # PyInstaller exe — project root is where the exe lives
        return Path(sys.executable).parent
    else:
        # Dev mode — desktop/ is one level below project root
        return Path(__file__).parent.parent


def get_bundled_node_dir() -> Optional[Path]:
    """Return path to the portable Node directory bundled by the installer
    (Windows only). None if not present or not on Windows."""
    if sys.platform != "win32":
        return None
    candidate = get_project_root() / "node"
    if (candidate / "node.exe").is_file():
        return candidate
    return None


# Where a Mac keeps the tools a launcher shells out to. Homebrew's own prefix
# first, Intel's second — both are checked because either can be the real one.
_MACOS_TOOL_DIRS = ("/opt/homebrew/bin", "/usr/local/bin")


def repair_gui_path() -> None:
    """Put Homebrew back on PATH when the launcher was started as a GUI app.

    Finder and the Dock hand a process /usr/bin:/bin:/usr/sbin:/sbin — a PATH
    that has never heard of Homebrew — while a terminal has already sourced a
    shell profile that puts it there. Every `shutil.which` in this process and
    every child that inherits this environment therefore answers differently
    depending on how the app was started, and the .app is the half where the
    answers are wrong: the wizard reports "Node.js not found" on a machine
    where node is installed and on PATH for its owner.

    Called once at launcher start, so the fix lands in one place instead of at
    every lookup that would otherwise have to know about Homebrew.
    """
    if sys.platform != "darwin":
        return
    current = os.environ.get("PATH", "").split(os.pathsep)
    missing = [d for d in _MACOS_TOOL_DIRS if os.path.isdir(d) and d not in current]
    if missing:
        os.environ["PATH"] = os.pathsep.join(missing + current)
        logger.info("PATH repaired for GUI launch: added %s", ", ".join(missing))


def get_node_executable() -> Optional[Path]:
    """Find a usable Node binary: bundled (Win) → PATH."""
    bundled = get_bundled_node_dir()
    if bundled is not None:
        return bundled / "node.exe"
    found = shutil.which("node")
    return Path(found) if found else None


def get_npm_executable() -> Optional[Path]:
    """Find npm alongside Node. Win: npm.cmd; Mac/Linux: npm."""
    bundled = get_bundled_node_dir()
    if bundled is not None:
        cand = bundled / "npm.cmd"
        if cand.is_file():
            return cand
    found = shutil.which("npm")
    if found:
        return Path(found)
    # Fallback: look next to node binary (some setups don't expose npm in PATH)
    node = get_node_executable()
    if node is not None:
        for name in ("npm.cmd", "npm"):
            cand = node.parent / name
            if cand.is_file():
                return cand
    return None


def detect_node_version() -> Optional[Tuple[int, int, int]]:
    """Return (major, minor, patch) from `node --version`, or None."""
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


def get_claude_prefix() -> Path:
    """Per-user prefix where `npm install` puts Claude Code so we don't
    pollute the user's global node_modules."""
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
    return base / "Sautium" / "claude-prefix"


def get_claude_executable() -> Optional[Path]:
    """Path to the native Claude Code binary, bypassing npm's shim.

    The .cmd shim on Windows runs through cmd.exe (8191-char arg limit),
    which truncates our ~13k system_prompt. The native binary
    (`bin/claude.exe`, same name on every platform — postinstall copies
    the platform-matched binary over the placeholder) takes the same args
    via CreateProcessW (32k limit). Lookup order: bundled prefix → npm
    shim in PATH redirected to its sibling .exe → raw shim as last resort."""
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


def detect_claude_cli() -> bool:
    """Check if Claude Code CLI is available (bundled prefix or PATH)."""
    return get_claude_executable() is not None


def claude_authenticated() -> bool:
    """True iff Claude Code has stored OAuth credentials.

    Storage is platform-dependent:
      - macOS: a generic password entry in the user's login Keychain
        named "Claude Code-credentials". (~/.claude/.credentials.json
        on macOS is a *directory* placeholder created by the CLI, so a
        file-based check is wrong here.)
      - Windows / Linux: a JSON file at ~/.claude/.credentials.json.
    """
    if sys.platform == "darwin":
        try:
            kwargs = {"capture_output": True, "timeout": 5}
            # The plain `find-generic-password` (without `-w`) only checks
            # for existence — it doesn't try to read the password and so
            # doesn't trigger a Keychain-unlock prompt. Exit 0 = present,
            # 44 (errSecItemNotFound) = absent.
            result = subprocess.run(
                ["security", "find-generic-password",
                 "-s", "Claude Code-credentials"],
                **kwargs,
            )
            return result.returncode == 0
        except Exception as e:
            logger.debug(f"Keychain probe failed: {e}")
            return False

    creds = Path.home() / ".claude" / ".credentials.json"
    if not creds.is_file():
        return False
    try:
        json.loads(creds.read_text(encoding="utf-8"))
        return True
    except (json.JSONDecodeError, OSError) as e:
        logger.debug(f"Claude credentials unreadable: {e}")
        return False


def claude_auth_verified(timeout: float = 90.0) -> bool:
    """Live end-to-end auth check: one minimal `-p` turn.

    `claude_authenticated()` only proves a credentials file EXISTS — stored
    tokens can be expired beyond refresh, revoked, or belong to a lapsed
    subscription, and the wizard used to treat that as signed-in, so the
    first chat died with "OAuth session expired and could not be refreshed".
    A short-expired access token is NOT such a case (the CLI refreshes
    lazily on use), so static inspection of expiresAt cannot distinguish
    working from dead credentials — only actually running the CLI can.

    Slow (seconds; up to `timeout` on a stalled network) — call from a
    worker thread."""
    claude = get_claude_executable()
    if claude is None:
        return False
    env = os.environ.copy()
    # The assistant runner drops ANTHROPIC_API_KEY so the CLI bills the OAuth
    # subscription — the probe must test the same path, not a stray key.
    env.pop("ANTHROPIC_API_KEY", None)
    kwargs = {
        "capture_output": True, "text": True,
        "encoding": "utf-8", "errors": "replace",
        "timeout": timeout, "env": env,
    }
    if sys.platform == "win32":
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    try:
        result = subprocess.run(
            [str(claude), "-p", "ok", "--output-format", "json"], **kwargs)
    except Exception as e:
        logger.debug(f"Claude auth probe failed to run: {e}")
        return False
    if result.returncode != 0:
        logger.info(f"Claude auth probe rc={result.returncode}: "
                    f"{(result.stderr or result.stdout or '').strip()[:200]}")
        return False
    try:
        out = json.loads(result.stdout.strip().splitlines()[-1])
        return not out.get("is_error", False)
    except (json.JSONDecodeError, IndexError):
        return True  # rc 0 with unparsable output — older CLI, trust the rc


def install_claude_runtime(progress_cb=None) -> Tuple[bool, str]:
    """`npm install --prefix <claude_prefix> @anthropic-ai/claude-code`.

    Long-running and network-dependent — call from a worker thread. The
    caller is responsible for verifying Node is present (use
    `detect_node_version()` first) and surfacing the returned message
    to the user on failure."""
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
    if progress_cb:
        progress_cb("Installing Claude Code via npm...")

    kwargs = {
        "capture_output": True, "text": True,
        "encoding": "utf-8", "errors": "replace",
        "timeout": 600,  # generous for slow networks
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
        # npm errors are verbose — keep the tail which usually has the cause.
        return False, err[-600:] if err else f"npm exited with code {result.returncode}"

    if get_claude_executable() is None:
        return False, "Install reported success but claude binary not found in prefix"

    return True, "Claude Code installed"


def launch_claude_setup() -> "subprocess.Popen":
    """Open `claude` in a new console/terminal window so the user can
    pick a theme and complete the OAuth login flow. Returns the Popen
    handle. Polls `claude_authenticated()` to know when the user is done."""
    claude = get_claude_executable()
    if claude is None:
        raise RuntimeError("Claude Code CLI not installed")

    if sys.platform == "win32":
        # `start` spins up a fresh console; `cmd /k` keeps it open after
        # claude exits so the user sees the final "logged in" message.
        return subprocess.Popen(
            ["cmd.exe", "/c", "start", "Claude Setup",
             "cmd.exe", "/k", str(claude)],
            creationflags=subprocess.CREATE_NEW_CONSOLE,
        )
    if sys.platform == "darwin":
        # AppleScript opens Terminal.app with the command. The path MUST be
        # shell-quoted: the bundled prefix lives under "Application Support"
        # and an unquoted space made zsh run "/Users/…/Library/Application"
        # (measured on the codex twin, 2026-08-23). Single quotes are safe
        # inside the AppleScript double-quoted literal.
        script = (
            f'tell application "Terminal"\n'
            f'  do script "\'{claude}\'"\n'
            f'  activate\n'
            f'end tell'
        )
        return subprocess.Popen(["osascript", "-e", script])
    raise RuntimeError("Interactive Claude setup not supported on this platform")


# ---------------------------------------------------------------------------
# OpenAI Codex CLI — the second subscription agent; mirrors the Claude
# block above. Backend mirror: backend/codex_cli.py (keep in sync).
# ---------------------------------------------------------------------------

def get_codex_prefix() -> Path:
    """Per-user prefix where `npm install` puts Codex — sibling of
    claude-prefix."""
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
    return base / "Sautium" / "codex-prefix"


def _codex_native_binary(node_modules: Path) -> Optional[Path]:
    """Native codex binary under a node_modules dir. The JS shim
    `bin/codex.js` spawns a platform binary from a separate platform
    package (`@openai/codex-<os>-<arch>/vendor/<triple>/bin/codex[.exe]`)
    — and npm places that package HOISTED next to @openai/codex on
    local `--prefix` installs but NESTED inside it on global installs
    (measured both, 2026-08-23; the nested-only assumption made the
    wizard report "binary not found in prefix" right after a successful
    install). Callers pass every candidate node_modules dir; the rust
    triple is globbed so target renames don't break lookup."""
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
    """Prefer the native binary (skips the node shim hop); a shim is an
    acceptable fallback — codex argv stays tiny (the assistant prompt travels
    as AGENTS.md on disk, not the command line, so cmd.exe's 8191-char
    cap never bites the way it did for claude). Shim priority: the
    prefix's own node_modules/.bin (survives any future vendor-layout
    change) before whatever is on PATH."""
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
        native = _codex_native_binary(nm)
        if native is not None:
            return native
    return shim_candidates[0] if shim_candidates else None


def detect_codex_cli() -> bool:
    """Check if the Codex CLI is available (bundled prefix or PATH)."""
    return get_codex_executable() is not None


def codex_authenticated() -> bool:
    """True iff codex has stored credentials ($CODEX_HOME/auth.json —
    ChatGPT OAuth or a stored API key). No Keychain variant exists;
    every platform uses the file."""
    home = os.environ.get("CODEX_HOME")
    auth = (Path(home) if home else Path.home() / ".codex") / "auth.json"
    if not auth.is_file():
        return False
    try:
        json.loads(auth.read_text(encoding="utf-8"))
        return True
    except (json.JSONDecodeError, OSError) as e:
        logger.debug(f"Codex credentials unreadable: {e}")
        return False


def codex_auth_verified(timeout: float = 90.0) -> bool:
    """Live end-to-end auth check: one minimal ephemeral exec turn.
    Same rationale as `claude_auth_verified` — auth.json can hold tokens
    revoked or expired beyond refresh, and only a real turn proves the
    account bills. No `-m`: the CLI's own default model is always a
    valid id, so the probe never rots when the catalog moves.

    Slow (seconds; up to `timeout` on a stalled network) — call from a
    worker thread."""
    import tempfile

    codex = get_codex_executable()
    if codex is None:
        return False
    env = os.environ.copy()
    # Mirror the runner: with auth.json present the env keys are dropped
    # so the probe tests the stored credential, not a stray API key.
    env.pop("OPENAI_API_KEY", None)
    env.pop("CODEX_API_KEY", None)
    kwargs = {
        "capture_output": True, "text": True,
        "encoding": "utf-8", "errors": "replace",
        "timeout": timeout, "env": env,
        "stdin": subprocess.DEVNULL,
    }
    if sys.platform == "win32":
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    try:
        result = subprocess.run(
            [str(codex), "exec", "--json", "--ephemeral",
             "--cd", tempfile.gettempdir(),
             "--skip-git-repo-check", "--ignore-user-config",
             "-c", 'model_reasoning_effort="low"',
             "--", "Reply with exactly: ok"],
            **kwargs)
    except Exception as e:
        logger.debug(f"Codex auth probe failed to run: {e}")
        return False
    turn_completed = False
    for line in (result.stdout or "").splitlines():
        try:
            evt = json.loads(line)
        except json.JSONDecodeError:
            continue
        if evt.get("type") == "turn.completed":
            turn_completed = True
        elif evt.get("type") == "turn.failed":
            logger.info(
                "Codex auth probe turn.failed: "
                f"{((evt.get('error') or {}).get('message') or '')[:200]}")
            return False
    return turn_completed


def install_codex_runtime(progress_cb=None) -> Tuple[bool, str]:
    """`npm install --prefix <codex_prefix> @openai/codex`. Long-running
    and network-dependent — call from a worker thread; caller verifies
    Node first (`detect_node_version()`)."""
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
    if progress_cb:
        progress_cb("Installing Codex via npm...")

    kwargs = {
        "capture_output": True, "text": True,
        "encoding": "utf-8", "errors": "replace",
        "timeout": 600,  # generous for slow networks
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


def launch_codex_setup() -> "subprocess.Popen":
    """Open `codex login` in a new console/terminal window — the CLI
    prints an auth URL and finishes the ChatGPT OAuth flow itself.
    Poll `codex_authenticated()` to know when the user is done."""
    codex = get_codex_executable()
    if codex is None:
        raise RuntimeError("Codex CLI not installed")

    if sys.platform == "win32":
        return subprocess.Popen(
            ["cmd.exe", "/c", "start", "Codex Setup",
             "cmd.exe", "/k", str(codex), "login"],
            creationflags=subprocess.CREATE_NEW_CONSOLE,
        )
    if sys.platform == "darwin":
        # Path shell-quoted — the prefix lives under "Application Support"
        # (see launch_claude_setup).
        script = (
            f'tell application "Terminal"\n'
            f'  do script "\'{codex}\' login"\n'
            f'  activate\n'
            f'end tell'
        )
        return subprocess.Popen(["osascript", "-e", script])
    raise RuntimeError("Interactive Codex setup not supported on this platform")


def detect_git() -> bool:
    """Check if git is available in PATH."""
    return shutil.which("git") is not None


def detect_gpu() -> Tuple[bool, Optional[str], Optional[float]]:
    """
    Detect GPU / AI accelerator.

    Checks NVIDIA CUDA first, then Apple Silicon (Metal / Neural Engine).

    Returns:
        (available, gpu_name, vram_gb_or_None)
    """
    # Try NVIDIA CUDA
    nvidia_smi = shutil.which("nvidia-smi")
    if nvidia_smi:
        try:
            result = subprocess.run(
                [nvidia_smi, "--query-gpu=name,memory.total", "--format=csv,noheader,nounits"],
                capture_output=True,
                text=True,
                timeout=10,
                creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
            )
            if result.returncode == 0 and result.stdout.strip():
                line = result.stdout.strip().split("\n")[0]
                parts = [p.strip() for p in line.split(",")]
                gpu_name = parts[0]
                vram_mb = float(parts[1])
                return True, gpu_name, round(vram_mb / 1024, 1)
        except Exception as e:
            logger.debug(f"nvidia-smi failed: {e}")

    # Apple Silicon (Metal + Neural Engine)
    if sys.platform == "darwin" and platform.machine() == "arm64":
        try:
            result = subprocess.run(
                ["sysctl", "-n", "machdep.cpu.brand_string"],
                capture_output=True, text=True, timeout=5,
            )
            chip_name = result.stdout.strip() if result.returncode == 0 else "Apple Silicon"
            # Get unified memory size
            result = subprocess.run(
                ["sysctl", "-n", "hw.memsize"],
                capture_output=True, text=True, timeout=5,
            )
            mem_gb = None
            if result.returncode == 0:
                mem_gb = round(int(result.stdout.strip()) / (1024 ** 3), 1)
            return True, f"{chip_name} (Metal)", mem_gb
        except Exception as e:
            logger.debug(f"Apple Silicon detection failed: {e}")
            return True, "Apple Silicon (Metal)", None

    return False, None, None


# Backward compatibility alias
detect_cuda = detect_gpu


def find_available_port(preferred: int) -> int:
    """
    Find an available TCP port, starting with the preferred one.

    Returns the preferred port if available, otherwise finds the next free one.
    """
    for port in range(preferred, preferred + 100):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.bind(("127.0.0.1", port))
                return port
        except OSError:
            continue
    raise RuntimeError(f"No available port found near {preferred}")


def get_local_ip() -> str:
    """Get the local network IP address for LAN access.

    This asks the kernel which source address it would use to reach the
    internet, so it returns the default route's interface. Tailscale does not
    take the default route, which is why it never shows up here — see
    get_tailscale_ip()."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"


_TAILSCALE_BINARIES = (
    r"C:\Program Files\Tailscale\tailscale.exe",
    "/Applications/Tailscale.app/Contents/MacOS/Tailscale",
    "/usr/local/bin/tailscale",
    "/usr/bin/tailscale",
    "tailscale",
)


_tailscale_cache: Tuple[float, Optional[str]] = (0.0, None)


def get_tailscale_ip() -> Optional[str]:
    """This node's Tailscale address, or None when Tailscale is not running.

    Asks the CLI rather than scanning interfaces, because the question is
    "is Tailscale up right now", not "is it installed" — and because an
    interface scan for 100.64/10 has a real false positive: a laptop tethered
    to a phone gets a carrier CGNAT address in exactly that range.

    Cached briefly: this spawns a process, and callers redraw on a timer."""
    global _tailscale_cache
    checked, cached = _tailscale_cache
    if time.monotonic() - checked < 60.0:
        return cached

    result = _probe_tailscale()
    _tailscale_cache = (time.monotonic(), result)
    return result


def _probe_tailscale() -> Optional[str]:
    for binary in _TAILSCALE_BINARIES:
        try:
            out = subprocess.run(
                [binary, "ip", "-4"], capture_output=True, text=True, timeout=5,
                creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        if out.returncode != 0:
            continue                      # installed but logged out / down
        ip = out.stdout.strip().splitlines()[0].strip() if out.stdout.strip() else ""
        if ip:
            return ip
    return None


def generate_qr_image(url: str, size: int = 200):
    """
    Generate a QR code as a PIL Image.

    Returns:
        PIL.Image.Image or None if qrcode package unavailable
    """
    try:
        import qrcode
        from qrcode.constants import ERROR_CORRECT_M

        qr = qrcode.QRCode(
            version=1,
            error_correction=ERROR_CORRECT_M,
            box_size=8,
            border=2,
        )
        qr.add_data(url)
        qr.make(fit=True)
        img = qr.make_image(fill_color="black", back_color="white")
        return img.resize((size, size))
    except ImportError:
        logger.warning("qrcode package not installed")
        return None


def generate_qr_ctk(url: str, size: int = 200):
    """
    Generate a QR code as a CTkImage for use in customtkinter.

    Returns:
        CTkImage or None
    """
    pil_img = generate_qr_image(url, size)
    if pil_img is None:
        return None

    try:
        import customtkinter as ctk
        return ctk.CTkImage(light_image=pil_img, dark_image=pil_img, size=(size, size))
    except ImportError:
        return None


def check_port_in_use(port: int) -> bool:
    """Check if a TCP port is currently in use."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", port))
            return False
    except OSError:
        return True


# ================================================================
# Idle-sleep inhibition (held while the node's services are up)
# ================================================================
# A suspended host is an offline node: playback stops mid-album, the DHT
# announces go stale, a CGNAT peer's wake subscription drops and nobody can
# reach it until someone wiggles the mouse. So the launcher holds off sleep
# for as long as it is actually running services.
#
# IDLE sleep only, and the display is left alone: a music node that keeps the
# monitor lit all night is a worse bug than the one being fixed. Deliberate
# sleep — lid, power button, Start menu — is untouched by both APIs, so the
# machine still obeys its owner.
_awake_release: Optional[threading.Event] = None


def _windows_stay_awake(release: threading.Event) -> None:
    """SetThreadExecutionState is PER-THREAD and dies with the thread that set
    it, so this thread must outlive the hold — it parks here until released
    (and process exit releases it just as well)."""
    import ctypes
    ES_CONTINUOUS = 0x80000000
    ES_SYSTEM_REQUIRED = 0x00000001
    kernel32 = ctypes.windll.kernel32
    if not kernel32.SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED):
        logger.warning("Could not inhibit sleep (SetThreadExecutionState failed)")
        return
    logger.info("Idle sleep inhibited while services run")
    release.wait()
    kernel32.SetThreadExecutionState(ES_CONTINUOUS)


def keep_awake(enabled: bool) -> None:
    """Hold (or drop) the idle-sleep inhibition. Idempotent — a second acquire
    is a no-op. Never fatal: a node that cannot inhibit sleep still works, it
    just sleeps."""
    global _awake_release
    if enabled == (_awake_release is not None):
        return

    if not enabled:
        _awake_release.set()          # Windows thread wakes; macOS caffeinate exits
        _awake_release = None
        logger.info("Idle sleep inhibition released")
        return

    release = threading.Event()
    if sys.platform == "win32":
        threading.Thread(target=_windows_stay_awake, args=(release,),
                         name="stay-awake", daemon=True).start()
    elif sys.platform == "darwin":
        # -i (PreventUserIdleSystemSleep), NOT -s: macOS scopes -s to AC power,
        # and that scoping cannot be trusted — a charge limiter like AlDente
        # discharges the battery with the charger plugged in, at which point the
        # system reports battery power and the assertion silently stops
        # applying. An AC gate that fails towards "quietly does nothing" is
        # worse than no gate; -i matches what Windows does here anyway. A
        # laptop on real battery keeps its usual protection: closing the lid
        # sleeps regardless. -w ties the assertion to our pid, so it lifts even
        # if the launcher is killed outright.
        try:
            proc = subprocess.Popen(["caffeinate", "-i", "-w", str(os.getpid())])
        except OSError as e:
            logger.warning("Could not inhibit sleep (caffeinate): %s", e)
            return
        threading.Thread(target=lambda: (release.wait(), proc.terminate()),
                         name="stay-awake", daemon=True).start()
        logger.info("Idle sleep inhibited while services run (caffeinate)")
    else:
        logger.debug("No idle-sleep inhibition on %s", sys.platform)
        return
    _awake_release = release
