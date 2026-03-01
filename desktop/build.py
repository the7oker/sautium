"""
PyInstaller build script for Music AI DJ launcher.

Builds a single-file executable for the launcher UI.

Usage:
    python desktop/build.py
"""

import importlib
import subprocess
import sys
from pathlib import Path


def _get_package_path(package_name: str) -> str:
    """Get the directory containing an installed package."""
    mod = importlib.import_module(package_name)
    return str(Path(mod.__file__).parent)


def build():
    project_root = Path(__file__).parent.parent
    desktop_dir = project_root / "desktop"
    icon_path = desktop_dir / "assets" / "icon.ico"

    # Create a simple entry script that imports and runs the launcher
    entry_script = project_root / "_launcher_entry.py"
    entry_script.write_text(
        "from desktop.launcher import main\nmain()\n"
    )

    # Get customtkinter path to bundle it explicitly
    ctk_path = _get_package_path("customtkinter")

    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--onefile",
        "--windowed",
        "--name", "MusicAIDJ",
        "--add-data", f"{desktop_dir / 'migrations'};desktop/migrations",
        "--add-data", f"{desktop_dir / 'assets'};desktop/assets",
        "--add-data", f"{ctk_path};customtkinter",
        "--hidden-import", "customtkinter",
        "--hidden-import", "pystray",
        "--hidden-import", "PIL",
        "--hidden-import", "PIL._tkinter_finder",
        "--hidden-import", "qrcode",
        "--hidden-import", "psutil",
        "--hidden-import", "psycopg2",
        "--hidden-import", "desktop",
        "--hidden-import", "desktop.launcher",
        "--hidden-import", "desktop.config_manager",
        "--hidden-import", "desktop.service_manager",
        "--hidden-import", "desktop.utils",
        "--hidden-import", "desktop.tray",
        "--hidden-import", "desktop.wizard",
        "--hidden-import", "desktop.settings",
        "--hidden-import", "desktop.updater",
        "--hidden-import", "desktop.db_init",
        "--collect-all", "customtkinter",
    ]

    if icon_path.exists():
        cmd.extend(["--icon", str(icon_path)])

    cmd.append(str(entry_script))

    print(f"Building launcher with PyInstaller...")
    print(f"Command: {' '.join(cmd)}")

    result = subprocess.run(cmd, cwd=str(project_root))

    # Clean up entry script
    entry_script.unlink(missing_ok=True)

    if result.returncode == 0:
        print("Build successful! Output in dist/MusicAIDJ.exe")
    else:
        print("Build failed!")
        sys.exit(1)


if __name__ == "__main__":
    build()
