r"""
Build Sautium-<version>-Setup.exe.

    python desktop/build_windows.py                 # stage + compile
    python desktop/build_windows.py --stage-only    # no Inno Setup needed
    python desktop/build_windows.py --compile-only  # iterate on the .iss over a stage

Runs on Windows or under WSL: staging is plain Python, and the compiler (Inno
Setup 6 — ISCC.exe, per-machine or per-user install) is found on either side
and handed Windows paths.

The installer is a carrier, like the macOS bundle: a private CPython
(python-build-standalone, Tk 8.6), a MinGit and a snapshot of the tree. The
launcher itself is not in it. desktop/windows/bootstrap.py clones main into
%LOCALAPPDATA%\Sautium\app on first run and starts `python -m desktop` from
there; from then on updates are the launcher's own, and a new Setup.exe only
ever carries a new runtime or a new git.

There is no code-signing certificate, so SmartScreen warns on the downloaded
Setup.exe ("More info" → "Run anyway"). The install is per-user and never
elevates, for the same reason — the UAC prompt for an unsigned binary is the
most alarming one Windows has — and because pip installs the launcher's
packages into the runtime under the install folder, which must therefore be
the user's own.
"""

import argparse
import os
import shutil
import subprocess
import sys
import urllib.request
import zipfile
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from desktop.build_common import (  # noqa: E402
    APP_NAME, BUILD_DIR, CACHE_DIR, DIST_DIR, VERSION, run, stage_payload,
    unpack_runtime,
)
from desktop.icon import write_ico  # noqa: E402

STAGE = BUILD_DIR / "windows"
ISS = Path(__file__).resolve().parent / "installer" / "sautium.iss"
BOOTSTRAP = Path(__file__).resolve().parent / "windows" / "bootstrap.py"

RUNTIME_TARGET = "x86_64-pc-windows-msvc"
RUNTIME_PRUNE_DIRS = ("Lib/test", "Lib/idlelib", "Lib/turtledemo")
# Debug symbols are more than half of the runtime (82 of 151 MB) and serve a
# debugger nobody attaches.
RUNTIME_PRUNE_GLOBS = ("**/*.pdb",)

# MinGit: Git for Windows without the shell, the GUI or the installer — what
# its authors publish for exactly this, an application carrying its own git.
# The tag and the asset version differ in shape; bump both together.
MINGIT_TAG = "v2.55.0.windows.5"
MINGIT_VERSION = "2.55.0.5"
MINGIT_URL = (
    f"https://github.com/git-for-windows/git/releases/download/{MINGIT_TAG}/"
    f"MinGit-{MINGIT_VERSION}-64-bit.zip"
)

# rcedit (Electron's resource editor, MIT — build tooling, not shipped)
# rewrites the copied stub's icon and version strings, so every surface that
# reads them — Task Manager's Processes tab, the firewall dialog's title,
# Explorer — says Sautium rather than Python, and OriginalFilename matches
# the file name (the mismatch is what "renamed binary" heuristics look for).
RCEDIT_VERSION = "2.0.0"
RCEDIT_URL = f"https://github.com/electron/rcedit/releases/download/v{RCEDIT_VERSION}/rcedit-x64.exe"

# Shown by Setup after the files are in place (InfoAfterFile): the moment the
# user is about to press "Launch Sautium" and the last screen before the app
# has to explain itself.
FIRST_LAUNCH_NOTE = r"""Sautium is installed. What happens next
=======================================

The first launch sets Sautium up. It happens once and takes a few minutes
on a good connection:

  - the current version is fetched from GitHub (the copy that came with
    this installer is the fallback when GitHub cannot be reached);
  - the launcher downloads its components: PostgreSQL 18, a Python for
    the backend, ffmpeg and the other audio tools;
  - Windows asks for administrator permission to open the firewall for
    the web player and the peer network — once per rule. Say no, and
    only this computer can reach the player.

The setup wizard then creates your account and the database. Its music
catalogue step downloads ~21 GB in the background — untick it if you just
want to look around. Finishing the wizard starts the backend, which
installs the ML stack on its first run (~1.3 GB once; the GPU build when
an NVIDIA card is present).

In the launcher window: "Choose Music Folder…" points Sautium at your
music, "Open Web UI" opens the player in your browser (plain HTTP on this
computer and your LAN; the launcher's QR code pairs a phone). Closing the
window keeps Sautium running in the tray; "Quit" is in the tray menu.

Sautium keeps everything in four places. Uninstalling removes the first
and asks about the others:

  %LOCALAPPDATA%\Programs\Sautium   this program: Python, git, the seed copy
  %LOCALAPPDATA%\Sautium            the app, database, logs, downloaded components
  %APPDATA%\Sautium                 settings and your account key
  %USERPROFILE%\.sautium            the browser certificate of earlier versions

Models (%USERPROFILE%\.cache\huggingface) and pip's cache are left alone.
"""


# ================================================================
# Staging
# ================================================================

def stage_runtime() -> None:
    runtime = STAGE / "runtime"
    unpack_runtime(RUNTIME_TARGET, runtime, RUNTIME_PRUNE_DIRS, RUNTIME_PRUNE_GLOBS)
    # A pythonw by another name — what every Electron app is to electron.exe.
    # CPython's exe is a stub that finds python312.dll and the stdlib beside
    # itself, so a copy runs identically, and the launcher then appears as
    # Sautium.exe in Task Manager, the firewall dialog and the taskbar
    # instead of pythonw.exe.
    launcher = runtime / f"{APP_NAME}.exe"
    shutil.copy2(runtime / "pythonw.exe", launcher)
    brand_launcher(launcher)


def fetch_rcedit() -> Path:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    tool = CACHE_DIR / "rcedit-x64.exe"
    if not tool.exists():
        print(f"rcedit: downloading {RCEDIT_URL}")
        urllib.request.urlretrieve(RCEDIT_URL, tool)
        tool.chmod(0o755)
    return tool


def brand_launcher(launcher: Path) -> None:
    """The stub's icon and version strings become Sautium's. LegalCopyright
    stays: the binary is CPython's, and the PSF licence keeps its notice on
    every copy."""
    if sys.platform != "win32" and not _under_wsl():
        print("  ! rcedit needs Windows or WSL — Sautium.exe keeps Python's icon and version strings")
        return
    ico = write_ico(STAGE / f"{APP_NAME}.ico")
    run([fetch_rcedit(), windows_path(launcher),
         "--set-icon", windows_path(ico),
         "--set-version-string", "FileDescription", APP_NAME,
         "--set-version-string", "ProductName", APP_NAME,
         "--set-version-string", "InternalName", APP_NAME,
         "--set-version-string", "CompanyName", APP_NAME,
         "--set-version-string", "OriginalFilename", f"{APP_NAME}.exe",
         "--set-file-version", VERSION,
         "--set-product-version", VERSION])
    print(f"Runtime: {launcher.name} branded (icon + version strings)")


def fetch_mingit() -> Path:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    archive = CACHE_DIR / f"MinGit-{MINGIT_VERSION}-64-bit.zip"
    if archive.exists():
        print(f"MinGit: cached {archive.name}")
        return archive
    print(f"MinGit: downloading {MINGIT_URL}")
    urllib.request.urlretrieve(MINGIT_URL, archive)
    return archive


def stage_git() -> None:
    target = STAGE / "git"
    shutil.rmtree(target, ignore_errors=True)
    with zipfile.ZipFile(fetch_mingit()) as archive:
        archive.extractall(target)
    if not (target / "cmd" / "git.exe").exists():
        raise SystemExit("MinGit archive has no cmd/git.exe — layout changed?")
    print(f"MinGit: staged {MINGIT_VERSION}")


def stage_static() -> None:
    shutil.copy2(BOOTSTRAP, STAGE / "bootstrap.py")
    write_ico(STAGE / f"{APP_NAME}.ico")
    (STAGE / "First launch.txt").write_text(FIRST_LAUNCH_NOTE, encoding="utf-8")


# ================================================================
# Compiling
# ================================================================

def _under_wsl() -> bool:
    return sys.platform == "linux" and Path("/proc/sys/fs/binfmt_misc/WSLInterop").exists()


def find_iscc() -> Optional[Path]:
    """Inno Setup's command-line compiler: on PATH, in Program Files, or in
    the per-user Programs folder (`/CURRENTUSER` install). Under WSL the same
    places, seen through /mnt/c."""
    if sys.platform == "win32":
        on_path = shutil.which("ISCC")
        if on_path:
            return Path(on_path)
        candidates = [
            Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")) / "Inno Setup 6" / "ISCC.exe",
            Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Inno Setup 6" / "ISCC.exe",
        ]
    elif _under_wsl():
        candidates = [
            Path("/mnt/c/Program Files (x86)/Inno Setup 6/ISCC.exe"),
            *Path("/mnt/c/Users").glob("*/AppData/Local/Programs/Inno Setup 6/ISCC.exe"),
        ]
    else:
        return None
    return next((path for path in candidates if path.exists()), None)


def windows_path(path: Path) -> str:
    if sys.platform == "win32":
        return str(path)
    return subprocess.run(["wslpath", "-w", str(path)], check=True,
                          capture_output=True, text=True).stdout.strip()


def compile_installer(iscc: Path) -> Path:
    DIST_DIR.mkdir(parents=True, exist_ok=True)
    run([iscc, "/Q",
         f"/DStageDir={windows_path(STAGE)}",
         f"/DVersion={VERSION}",
         f"/DOutputDir={windows_path(DIST_DIR)}",
         windows_path(ISS)])
    setup = DIST_DIR / f"{APP_NAME}-{VERSION}-Setup.exe"
    if not setup.exists():
        raise SystemExit(f"ISCC reported success but {setup} is missing")
    return setup


# ================================================================

def main() -> None:
    parser = argparse.ArgumentParser(description=f"Build {APP_NAME}-<version>-Setup.exe")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--stage-only", action="store_true",
                      help="stage into build/windows without running Inno Setup")
    mode.add_argument("--compile-only", action="store_true",
                      help="run Inno Setup over the existing stage (the runtime "
                           "and git are slow to re-stage; bootstrap.py and the "
                           "icon are refreshed)")
    args = parser.parse_args()

    iscc = None if args.stage_only else find_iscc()
    if not args.stage_only and iscc is None:
        raise SystemExit(
            "Inno Setup 6 not found — install it (https://jrsoftware.org/isdl.php, "
            "\"for me only\" is enough) or pass --stage-only")

    STAGE.mkdir(parents=True, exist_ok=True)
    if args.compile_only:
        if not (STAGE / "runtime" / f"{APP_NAME}.exe").exists():
            raise SystemExit(f"nothing staged in {STAGE} — run without --compile-only first")
        stage_static()
    else:
        build_id = stage_payload(STAGE / "payload")
        stage_runtime()
        stage_git()
        stage_static()
        size = sum(f.stat().st_size for f in STAGE.rglob("*") if f.is_file()) / 1e6
        print(f"\n{STAGE}  ({size:.0f} MB staged, build {build_id})")

    if iscc is None:
        return
    setup = compile_installer(iscc)
    print(f"{setup}  ({setup.stat().st_size / 1e6:.0f} MB)")


if __name__ == "__main__":
    main()
