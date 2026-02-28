"""
PyInstaller build script for Music AI DJ launcher.

Builds a single-file executable for the launcher UI.

Usage:
    python desktop/build.py
"""

import subprocess
import sys
from pathlib import Path


def build():
    project_root = Path(__file__).parent.parent
    desktop_dir = project_root / "desktop"
    icon_path = desktop_dir / "assets" / "icon.ico"

    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--onefile",
        "--windowed",
        "--name", "MusicAIDJ",
        "--add-data", f"{desktop_dir / 'migrations'};desktop/migrations",
        "--add-data", f"{desktop_dir / 'assets'};desktop/assets",
        "--hidden-import", "customtkinter",
        "--hidden-import", "pystray",
        "--hidden-import", "PIL",
        "--hidden-import", "qrcode",
        "--hidden-import", "psutil",
        "--hidden-import", "psycopg2",
        "--collect-all", "customtkinter",
    ]

    if icon_path.exists():
        cmd.extend(["--icon", str(icon_path)])

    cmd.append(str(desktop_dir / "launcher.py"))

    print(f"Building launcher with PyInstaller...")
    print(f"Command: {' '.join(cmd)}")

    result = subprocess.run(cmd, cwd=str(project_root))
    if result.returncode == 0:
        print("Build successful! Output in dist/MusicAIDJ.exe")
    else:
        print("Build failed!")
        sys.exit(1)


if __name__ == "__main__":
    build()
