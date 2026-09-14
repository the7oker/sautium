"""
What the macOS and Windows builds share.

Both packages are carriers, not frozen launchers: a private CPython
(python-build-standalone) plus a snapshot of the git-tracked tree, installed
on first run by a platform bootstrap that then runs `python -m desktop` from a
writable copy. PyInstaller was the other candidate and loses on the thing that
matters here: the launcher provisions and then RUNS a Python — pip-installing
torch, spawning uvicorn and the MCP server — and inside a frozen bundle
`sys.executable` is the bundle, not an interpreter that can do any of that.

build_macos.py and build_windows.py add the platform shape — the .app and
DMG, the Inno Setup installer — around the pieces here.
"""

import hashlib
import shutil
import subprocess
import tarfile
import urllib.request
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
BUILD_DIR = PROJECT_ROOT / "build"
CACHE_DIR = BUILD_DIR / "cache"
DIST_DIR = PROJECT_ROOT / "dist"

APP_NAME = "Sautium"
VERSION = "0.1.0"

# python-build-standalone: relocatable CPython with tkinter and its own OpenSSL.
# Bump both together — the URL embeds each.
#
# Pinned to the last release built against Tcl/Tk 8.6. CustomTkinter draws its
# rounded widgets as canvas polygons, and 5.2 does that in a way Tk 9.0 does not
# survive: from a terminal it raises `expected floating-point number but got
# "None"` out of canvas coords, and launched through LaunchServices the same
# state reaches C and segfaults in ConfigurePolygon — the app dies before its
# window appears. Homebrew's python@3.12 (what the launcher is developed on)
# carries Tk 8.6, so this pin is also what keeps the shipped app and the
# maintainer's own runs on the same toolkit.
PBS_RELEASE = "20251209"
PBS_PYTHON = "3.12.12"

# Written into a staged runtime; what a bootstrap compares against the copy
# it installed (macOS) or keys its dependency marker on (Windows).
RUNTIME_STAMP = "runtime.version"

# What the launcher and the backend import at runtime. `mcp/` is not optional:
# config_manager points the assistant MCP server at <project_root>/mcp.
PAYLOAD_ROOTS = ("backend", "desktop", "mcp")

# Never ship a maintainer's credentials in a friend's package. git-tracked
# enumeration already excludes these (all are gitignored); the sweep is the
# assertion that says so out loud if that ever stops being true.
SECRET_PATTERNS = (
    ".env", ".api_secret", ".node_key", "mcp-windows.json",
    "birth_certificate.json", "identity_proof.json", "*.pem", "*.key",
)


def run(cmd: list, **kwargs) -> subprocess.CompletedProcess:
    print("  $", " ".join(str(part) for part in cmd))
    return subprocess.run([str(part) for part in cmd], check=True, **kwargs)


# ================================================================
# Runtime
# ================================================================

def runtime_stamp() -> str:
    return f"{PBS_PYTHON}+{PBS_RELEASE}"


def runtime_url(target: str) -> str:
    """`target` is the Rust-style triple python-build-standalone names its
    assets by: aarch64-apple-darwin, x86_64-apple-darwin,
    x86_64-pc-windows-msvc."""
    return (
        f"https://github.com/astral-sh/python-build-standalone/releases/download/"
        f"{PBS_RELEASE}/cpython-{PBS_PYTHON}+{PBS_RELEASE}-{target}-install_only.tar.gz"
    )


def fetch_runtime(target: str) -> Path:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    archive = CACHE_DIR / f"cpython-{PBS_PYTHON}+{PBS_RELEASE}-{target}.tar.gz"
    if archive.exists():
        print(f"Runtime: cached {archive.name}")
        return archive
    url = runtime_url(target)
    print(f"Runtime: downloading {url}")
    urllib.request.urlretrieve(url, archive)
    return archive


def unpack_runtime(target: str, destination: Path, prune_dirs: tuple = (),
                   prune_globs: tuple = ()) -> None:
    """Extract the archive's `python/` tree to `destination`, drop what a
    launcher never imports, and stamp it."""
    shutil.rmtree(destination, ignore_errors=True)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(fetch_runtime(target)) as tar:
        tar.extractall(destination.parent, filter="data")
    (destination.parent / "python").rename(destination)
    for relative in prune_dirs:
        shutil.rmtree(destination / relative, ignore_errors=True)
    for pattern in prune_globs:
        for path in destination.glob(pattern):
            path.unlink()
    for cache in destination.rglob("__pycache__"):
        shutil.rmtree(cache, ignore_errors=True)
    (destination / RUNTIME_STAMP).write_text(runtime_stamp() + "\n", encoding="utf-8")
    print(f"Runtime: staged CPython {PBS_PYTHON} ({target})")


# ================================================================
# Payload
# ================================================================

def tracked_files() -> list:
    """git-tracked paths under the payload roots, read from the WORKING tree.

    Tracking is the filter — everything a build must not ship (secrets, caches,
    pgdata, the maintainer's mcp-windows.json) is already gitignored — while the
    content comes from disk so an uncommitted fix still makes it into the
    package.
    """
    result = run(["git", "-C", PROJECT_ROOT, "ls-files", "--", *PAYLOAD_ROOTS],
                 capture_output=True, text=True)
    return [line for line in result.stdout.splitlines() if line]


def warn_untracked() -> None:
    result = run(["git", "-C", PROJECT_ROOT, "ls-files", "--others",
                  "--exclude-standard", "--", *PAYLOAD_ROOTS],
                 capture_output=True, text=True)
    untracked = [line for line in result.stdout.splitlines() if line]
    if untracked:
        print("  ! untracked, NOT shipped:")
        for path in untracked:
            print(f"      {path}")


def stage_payload(payload: Path) -> str:
    """Copy the tracked tree to `payload` and stamp it with the build id the
    bootstraps compare and the launcher shows in its title. Returns the id."""
    shutil.rmtree(payload, ignore_errors=True)
    digest = hashlib.sha256()
    count = 0
    for relative in tracked_files():
        source = PROJECT_ROOT / relative
        if not source.exists():          # deleted in the working tree
            continue
        destination = payload / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
        digest.update(relative.encode())
        digest.update(source.read_bytes())
        count += 1
    warn_untracked()

    for pattern in SECRET_PATTERNS:
        found = list(payload.rglob(pattern))
        if found:
            raise SystemExit(f"refusing to ship secrets: {found}")

    build_id = f"{VERSION}+{digest.hexdigest()[:12]}"
    (payload / ".sautium_build").write_text(build_id + "\n", encoding="utf-8")
    print(f"Payload: {count} files, build {build_id}")
    return build_id
