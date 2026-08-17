"""
PyInstaller build script for Sautium launcher.

Builds a standalone application:
  - Windows: dist/Sautium.exe  (single-file)
  - macOS:   dist/Sautium.app  (.app bundle)

Usage:
    python desktop/build.py
"""

import importlib
import subprocess
import sys
from pathlib import Path

IS_MACOS = sys.platform == "darwin"
IS_WINDOWS = sys.platform == "win32"

# PyInstaller --add-data separator: `;` on Windows, `:` on macOS/Linux
SEP = ";" if IS_WINDOWS else ":"


def _get_package_path(package_name: str) -> str:
    """Get the directory containing an installed package."""
    mod = importlib.import_module(package_name)
    return str(Path(mod.__file__).parent)


def build():
    project_root = Path(__file__).parent.parent
    desktop_dir = project_root / "desktop"

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
        "--name", "Sautium",
        "--add-data", f"{desktop_dir / 'migrations'}{SEP}desktop/migrations",
        "--add-data", f"{ctk_path}{SEP}customtkinter",
        "--hidden-import", "customtkinter",
        "--hidden-import", "pystray",
        "--hidden-import", "PIL",
        "--hidden-import", "PIL._tkinter_finder",
        "--hidden-import", "qrcode",
        "--hidden-import", "psutil",
        "--hidden-import", "psycopg2",
        "--hidden-import", "desktop",
        "--hidden-import", "desktop.launcher",
        "--hidden-import", "desktop.__main__",
        "--hidden-import", "desktop.config_manager",
        "--hidden-import", "desktop.service_manager",
        "--hidden-import", "desktop.utils",
        "--hidden-import", "desktop.tray",
        "--hidden-import", "desktop.wizard",
        "--hidden-import", "desktop.settings",
        "--hidden-import", "desktop.updater",
        "--hidden-import", "desktop.db_init",
        "--hidden-import", "desktop.python_env",
        "--hidden-import", "desktop.p2p",
        "--hidden-import", "desktop.p2p.sync_queries",
        "--hidden-import", "desktop.p2p.sync_server",
        "--hidden-import", "desktop.p2p.dht_service",
        "--hidden-import", "desktop.p2p.p2p_manager",
        "--hidden-import", "desktop.p2p.birth_cert",
        "--hidden-import", "desktop.p2p.identity_pow",
        "--hidden-import", "desktop.p2p.identity_proof",
        "--hidden-import", "desktop.sync_client",
        "--hidden-import", "aiohttp",
        "--collect-all", "customtkinter",
    ]

    # Assets (icon, etc.) — only if directory exists
    assets_dir = desktop_dir / "assets"
    if assets_dir.exists():
        cmd.extend(["--add-data", f"{assets_dir}{SEP}desktop/assets"])

    # Platform-specific icon
    if IS_WINDOWS:
        icon_path = assets_dir / "icon.ico"
        if icon_path.exists():
            cmd.extend(["--icon", str(icon_path)])
    elif IS_MACOS:
        icon_path = assets_dir / "icon.icns"
        if icon_path.exists():
            cmd.extend(["--icon", str(icon_path)])
        # macOS bundle identifier
        cmd.extend(["--osx-bundle-identifier", "net.sautium.launcher"])

    # Bundle libtorrent if installed (C++ extension for DHT)
    try:
        import libtorrent
        cmd.extend(["--hidden-import", "libtorrent"])
        print(f"  libtorrent found: {Path(libtorrent.__file__).parent}")
    except ImportError:
        print("  WARNING: libtorrent not installed — DHT will be disabled in build")

    cmd.append(str(entry_script))

    print("Building Sautium launcher with PyInstaller...")
    print(f"Platform: {'macOS' if IS_MACOS else 'Windows' if IS_WINDOWS else 'Linux'}")
    print(f"Command: {' '.join(cmd)}")

    result = subprocess.run(cmd, cwd=str(project_root))

    # Clean up entry script
    entry_script.unlink(missing_ok=True)

    if result.returncode == 0:
        if IS_MACOS:
            print("\nBuild successful! Output: dist/Sautium.app")
            print("To create a DMG installer: hdiutil create -volname Sautium "
                  "-srcfolder dist/Sautium.app -ov dist/Sautium.dmg")
        else:
            print("\nBuild successful! Output: dist/Sautium.exe")
    else:
        print("Build failed!")
        sys.exit(1)


if __name__ == "__main__":
    build()
