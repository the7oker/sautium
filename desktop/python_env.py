"""
Download and manage embedded Python 3.12 for the backend.

PyTorch requires Python <=3.12, so we bundle an embedded Python
separately from whatever Python runs the launcher.
"""

import logging
import os
import subprocess
import sys
import urllib.request
import zipfile
from pathlib import Path
from typing import Optional, Callable

logger = logging.getLogger(__name__)

PYTHON_VERSION = "3.12.9"
PYTHON_DOWNLOAD_URL = (
    f"https://www.python.org/ftp/python/{PYTHON_VERSION}/"
    f"python-{PYTHON_VERSION}-embed-amd64.zip"
)
GET_PIP_URL = "https://bootstrap.pypa.io/get-pip.py"


def get_python_dir() -> Path:
    """Return the directory where embedded Python is installed."""
    from desktop.utils import get_project_root
    return get_project_root() / "python312"


def get_backend_python() -> str:
    """Return path to the Python executable for the backend.

    Falls back to sys.executable if embedded Python is not available
    (e.g., on Linux/macOS where system Python 3.12 may be fine).
    """
    if sys.platform == "win32":
        exe = get_python_dir() / "python.exe"
        if exe.exists():
            return str(exe)
    return sys.executable


def is_python_ready() -> bool:
    """Check if embedded Python is downloaded and pip is available."""
    python_dir = get_python_dir()
    python_exe = python_dir / "python.exe"
    if not python_exe.exists():
        return False
    # Check pip is installed
    pip_marker = python_dir / "Lib" / "site-packages" / "pip"
    return pip_marker.exists()


def download_embedded_python(progress_cb: Optional[Callable] = None) -> bool:
    """Download and set up embedded Python 3.12 for Windows."""
    python_dir = get_python_dir()
    python_exe = python_dir / "python.exe"

    if is_python_ready():
        logger.info("Embedded Python 3.12 already present")
        return True

    zip_path = python_dir.parent / "_python_download.zip"

    try:
        # Step 1: Download Python embeddable package
        if not python_exe.exists():
            if progress_cb:
                progress_cb("Downloading Python 3.12 (~15 MB)...")

            python_dir.mkdir(parents=True, exist_ok=True)

            def _reporthook(block_num, block_size, total_size):
                if total_size > 0 and progress_cb:
                    downloaded = block_num * block_size
                    pct = min(100, downloaded * 100 // total_size)
                    mb_down = downloaded // (1024 * 1024)
                    mb_total = total_size // (1024 * 1024)
                    progress_cb(
                        f"Downloading Python 3.12... {mb_down}/{mb_total} MB ({pct}%)"
                    )

            urllib.request.urlretrieve(
                PYTHON_DOWNLOAD_URL, str(zip_path), reporthook=_reporthook
            )

            if progress_cb:
                progress_cb("Extracting Python 3.12...")

            with zipfile.ZipFile(zip_path, "r") as zf:
                zf.extractall(str(python_dir))

            logger.info(f"Python 3.12 extracted to {python_dir}")

        # Step 2: Enable site-packages by editing ._pth file
        _enable_site_packages(python_dir)

        # Step 3: Install pip
        if progress_cb:
            progress_cb("Installing pip...")

        if not _install_pip(python_dir, progress_cb):
            return False

        if progress_cb:
            progress_cb("Python 3.12 ready!")

        return True

    except Exception as e:
        logger.error(f"Failed to set up Python 3.12: {e}")
        if progress_cb:
            progress_cb(f"Python 3.12 setup failed: {e}")
        return False

    finally:
        zip_path.unlink(missing_ok=True)


def _enable_site_packages(python_dir: Path) -> None:
    """Uncomment 'import site' in the ._pth file to enable pip/packages."""
    pth_files = list(python_dir.glob("python*._pth"))
    if not pth_files:
        logger.warning("No ._pth file found in Python directory")
        return

    pth_file = pth_files[0]
    content = pth_file.read_text(encoding="utf-8")

    if "#import site" in content:
        content = content.replace("#import site", "import site")
        # Also add Lib/site-packages path
        if "Lib\\site-packages" not in content:
            content += "\nLib\\site-packages\n"
        pth_file.write_text(content, encoding="utf-8")
        logger.info(f"Enabled site-packages in {pth_file.name}")
    elif "import site" in content:
        logger.info("site-packages already enabled")


def _install_pip(python_dir: Path, progress_cb: Optional[Callable] = None) -> bool:
    """Download and run get-pip.py to install pip."""
    python_exe = python_dir / "python.exe"
    get_pip_path = python_dir / "get-pip.py"

    # Check if pip already works
    check = subprocess.run(
        [str(python_exe), "-m", "pip", "--version"],
        capture_output=True, text=True,
        creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
    )
    if check.returncode == 0:
        logger.info(f"pip already installed: {check.stdout.strip()}")
        return True

    try:
        if progress_cb:
            progress_cb("Downloading pip installer...")

        urllib.request.urlretrieve(GET_PIP_URL, str(get_pip_path))

        if progress_cb:
            progress_cb("Installing pip (may take a minute)...")

        kwargs = {"capture_output": True, "text": True, "timeout": 300}
        if sys.platform == "win32":
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW

        result = subprocess.run(
            [str(python_exe), str(get_pip_path), "--quiet"],
            **kwargs,
        )

        if result.returncode != 0:
            logger.error(f"get-pip.py failed: {result.stderr[:500]}")
            return False

        logger.info("pip installed successfully")
        return True

    except Exception as e:
        logger.error(f"Failed to install pip: {e}")
        return False

    finally:
        get_pip_path.unlink(missing_ok=True)
