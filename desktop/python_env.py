"""
Download and manage embedded Python 3.12 for the backend.

PyTorch requires Python <=3.12, so we bundle an embedded Python
separately from whatever Python runs the launcher.
"""

import logging
import subprocess
import sys
import zipfile
from pathlib import Path
from typing import Optional, Callable

from desktop.downloads import ARTIFACTS, fetch

logger = logging.getLogger(__name__)


def get_python_dir() -> Path:
    """Return the directory where embedded Python is installed."""
    from desktop.utils import get_project_root
    return get_project_root() / "python312"


PROJECT_PTH = "sautium-project.pth"


def ensure_project_pth() -> None:
    """Put the backend dir and the project root on the embedded interpreter's
    sys.path. Its `python312._pth` pins the path to the interpreter's own
    dirs — the current directory and PYTHONPATH are ignored — so neither
    `python -m backup` run from backend/ nor the backend's own `desktop.*`
    imports resolve by themselves (the backend has ridden on routers/sync.py
    inserting the project root at import time). A .pth in site-packages is
    the one place a ._pth interpreter still takes extra paths from; entries
    are relative to the site-packages dir, so the checkout can move.
    Idempotent; a no-op for a non-embedded interpreter."""
    python_dir = get_python_dir()
    site = python_dir / "Lib" / "site-packages"
    if not list(python_dir.glob("python*._pth")) or not site.is_dir():
        return
    content = "..\\..\\..\\backend\n..\\..\\..\n"
    path = site / PROJECT_PTH
    if not path.exists() or path.read_text(encoding="utf-8") != content:
        path.write_text(content, encoding="utf-8")
        logger.info("Wrote %s (backend + project root on the embedded sys.path)", path.name)


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

            def _progress(done, total):
                if total > 0 and progress_cb:
                    pct = min(100, done * 100 // total)
                    progress_cb(f"Downloading Python 3.12... {done // (1024 * 1024)}/"
                                f"{total // (1024 * 1024)} MB ({pct}%)")

            fetch("python", zip_path, _progress)

            if progress_cb:
                progress_cb("Extracting Python 3.12...")

            with zipfile.ZipFile(zip_path, "r") as zf:
                zf.extractall(str(python_dir))

            logger.info(f"Python 3.12 extracted to {python_dir}")

        # Step 2: Enable site-packages by editing ._pth file, and put the
        # project on the path the ._pth would otherwise pin shut.
        _enable_site_packages(python_dir)
        ensure_project_pth()

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


# pip run out of its own wheel, installing that wheel with no index. Its main
# is called from -c: started as `<wheel>/pip`, pip on Windows refuses to
# modify itself unless run as `-m pip`, which the embedded interpreter cannot
# do before pip is installed (its ._pth ignores PYTHONPATH).
_PIP_FROM_WHEEL = ("import sys; sys.path.insert(0, sys.argv[1]); "
                   "from pip._internal.cli.main import main; "
                   "sys.exit(main(['install', '--no-index', '--quiet', '--disable-pip-version-check', "
                   "'--no-warn-script-location', sys.argv[1]]))")


def _install_pip(python_dir: Path, progress_cb: Optional[Callable] = None) -> bool:
    """Install pip from its own wheel, the one this release pins."""
    python_exe = python_dir / "python.exe"
    # pip takes a wheel only under its own name: name-version-tags.whl
    wheel = python_dir / ARTIFACTS["pip"].url.rsplit("/", 1)[1]

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
            progress_cb("Downloading pip...")

        fetch("pip", wheel)

        if progress_cb:
            progress_cb("Installing pip (may take a minute)...")

        kwargs = {"capture_output": True, "text": True, "timeout": 300}
        if sys.platform == "win32":
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW

        result = subprocess.run([str(python_exe), "-c", _PIP_FROM_WHEEL, str(wheel)], **kwargs)

        if result.returncode != 0:
            logger.error(f"pip install from its wheel failed: {result.stderr[:500]}")
            return False

        logger.info("pip installed successfully")
        return True

    except Exception as e:
        logger.error(f"Failed to install pip: {e}")
        return False

    finally:
        wheel.unlink(missing_ok=True)
