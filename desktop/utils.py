"""
Utility functions for Music AI DJ desktop launcher.
"""

import logging
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


def detect_claude_cli() -> bool:
    """Check if Claude Code CLI is available in PATH or via WSL."""
    if shutil.which("claude") is not None:
        return True

    # On Windows, check if claude is available inside WSL
    if sys.platform == "win32":
        try:
            result = subprocess.run(
                ["wsl", "claude", "--version"],
                capture_output=True, text=True, timeout=10,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            if result.returncode == 0 and "claude" in result.stdout.lower():
                return True
        except Exception:
            pass

    return False


def detect_git() -> bool:
    """Check if git is available in PATH."""
    return shutil.which("git") is not None


def detect_cuda() -> Tuple[bool, Optional[str], Optional[float]]:
    """
    Detect CUDA GPU via nvidia-smi.

    Returns:
        (available, gpu_name, vram_gb)
    """
    nvidia_smi = shutil.which("nvidia-smi")
    if not nvidia_smi:
        return False, None, None

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

    return False, None, None


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
