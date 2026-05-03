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
        # AppleScript opens Terminal.app with the command. Quoting: claude
        # path is absolute and doesn't contain double quotes by convention,
        # so embedding it directly is safe.
        script = (
            f'tell application "Terminal"\n'
            f'  do script "{claude}"\n'
            f'  activate\n'
            f'end tell'
        )
        return subprocess.Popen(["osascript", "-e", script])
    raise RuntimeError("Interactive Claude setup not supported on this platform")


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
    """Get the local network IP address for LAN access."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"


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
