"""
Build Sautium.app and its DMG.

    python desktop/build_macos.py               # host arch, ad-hoc signature
    python desktop/build_macos.py --arch x86_64
    python desktop/build_macos.py --sign "Developer ID Application: …" \
                                 --notarize <keychain-profile>

The bundle is NOT a frozen launcher. It carries a private CPython plus a
snapshot of the tree, and `Contents/Resources/bootstrap.py` installs both into
the launcher's data root on first run (see that file for why). PyInstaller was
the other candidate and loses on the thing that matters here: the launcher
provisions and then RUNS a Python — pip-installing torch, spawning uvicorn and
the MCP server — and inside a frozen bundle `sys.executable` is the bundle, not
an interpreter that can do any of that.
"""

import argparse
import hashlib
import plistlib
import shutil
import subprocess
import sys
import tarfile
import urllib.request
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
BUILD_DIR = PROJECT_ROOT / "build" / "macos"
CACHE_DIR = PROJECT_ROOT / "build" / "cache"
DIST_DIR = PROJECT_ROOT / "dist"

APP_NAME = "Sautium"
BUNDLE_ID = "net.sautium.launcher"
VERSION = "0.1.0"
MIN_MACOS = "12.0"

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

# What the launcher and the backend import at runtime. `mcp/` is not optional:
# config_manager points the assistant MCP server at <project_root>/mcp.
PAYLOAD_ROOTS = ("backend", "desktop", "mcp")

# Never ship a maintainer's credentials in a friend's DMG. git-tracked
# enumeration already excludes these (all are gitignored); the sweep is the
# assertion that says so out loud if that ever stops being true.
SECRET_PATTERNS = (
    ".env", ".api_secret", ".node_key", "mcp-windows.json",
    "birth_certificate.json", "identity_proof.json", "*.pem", "*.key",
)

RUNTIME_PRUNE = ("lib/python3.12/idlelib", "lib/python3.12/turtledemo",
                 "lib/python3.12/test", "share/man")


def run(cmd: list, **kwargs) -> subprocess.CompletedProcess:
    print("  $", " ".join(str(part) for part in cmd))
    return subprocess.run([str(part) for part in cmd], check=True, **kwargs)


# ================================================================
# Runtime
# ================================================================

def runtime_url(arch: str) -> str:
    machine = "aarch64" if arch == "arm64" else "x86_64"
    return (
        f"https://github.com/astral-sh/python-build-standalone/releases/download/"
        f"{PBS_RELEASE}/cpython-{PBS_PYTHON}+{PBS_RELEASE}-{machine}-apple-darwin-"
        f"install_only.tar.gz"
    )


def fetch_runtime(arch: str) -> Path:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    archive = CACHE_DIR / f"cpython-{PBS_PYTHON}-{arch}.tar.gz"
    if archive.exists():
        print(f"Runtime: cached {archive.name}")
        return archive
    url = runtime_url(arch)
    print(f"Runtime: downloading {url}")
    urllib.request.urlretrieve(url, archive)
    return archive


def stage_runtime(app: Path, arch: str) -> None:
    target = app / "Contents" / "Resources" / "runtime"
    with tarfile.open(fetch_runtime(arch)) as tar:
        tar.extractall(target.parent, filter="data")
    (target.parent / "python").rename(target)
    for relative in RUNTIME_PRUNE:
        shutil.rmtree(target / relative, ignore_errors=True)
    for cache in target.rglob("__pycache__"):
        shutil.rmtree(cache, ignore_errors=True)
    # What bootstrap.py compares against its installed copy.
    (target / "runtime.version").write_text(f"{PBS_PYTHON}+{PBS_RELEASE}\n", encoding="utf-8")
    print(f"Runtime: staged CPython {PBS_PYTHON} ({arch})")


# ================================================================
# Payload
# ================================================================

def tracked_files() -> list:
    """git-tracked paths under the payload roots, read from the WORKING tree.

    Tracking is the filter — everything a build must not ship (secrets, caches,
    pgdata, the maintainer's mcp-windows.json) is already gitignored — while the
    content comes from disk so an uncommitted fix still makes it into the DMG.
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


def stage_payload(app: Path) -> str:
    payload = app / "Contents" / "Resources" / "payload"
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


# ================================================================
# Icon
# ================================================================

def _blend(low: str, high: str, t: float) -> tuple:
    a = tuple(int(low[i:i + 2], 16) for i in (1, 3, 5))
    b = tuple(int(high[i:i + 2], 16) for i in (1, 3, 5))
    return tuple(round(a[i] + (b[i] - a[i]) * t) for i in range(3))


def render_icon(size: int = 1024):
    from PIL import Image, ImageDraw

    plate = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    gradient = Image.new("RGB", (1, size))
    for y in range(size):
        gradient.putpixel((0, y), _blend("#332B26", "#1B1714", y / (size - 1)))
    gradient = gradient.resize((size, size))

    margin = round(size * 0.098)
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        (margin, margin, size - margin - 1, size - margin - 1),
        radius=round(size * 0.185), fill=255,
    )
    plate.paste(gradient, (0, 0), mask)

    draw = ImageDraw.Draw(plate)
    bar_width = size * 0.062
    gap = size * 0.043
    heights = (0.20, 0.33, 0.47, 0.29, 0.21)
    # Depth comes from pre-blended colour, not alpha: ImageDraw writes RGBA
    # straight into the pixel, so a translucent bar would be translucent in the
    # finished icon and take its shade from whatever wallpaper sits behind it.
    depths = (0.45, 0.8, 1.0, 0.8, 0.45)
    total = len(heights) * bar_width + (len(heights) - 1) * gap
    x = (size - total) / 2
    centre = size / 2
    for height, depth in zip(heights, depths):
        half = size * height / 2
        draw.rounded_rectangle(
            (x, centre - half, x + bar_width, centre + half),
            radius=bar_width / 2,
            fill=_blend("#241F1B", "#E8B06F", depth) + (255,),
        )
        x += bar_width + gap
    return plate


def stage_icon(app: Path) -> None:
    iconset = BUILD_DIR / f"{APP_NAME}.iconset"
    shutil.rmtree(iconset, ignore_errors=True)
    iconset.mkdir(parents=True)
    master = render_icon()
    from PIL import Image
    for base in (16, 32, 128, 256, 512):
        master.resize((base, base), Image.LANCZOS).save(iconset / f"icon_{base}x{base}.png")
        master.resize((base * 2, base * 2), Image.LANCZOS).save(
            iconset / f"icon_{base}x{base}@2x.png")
    run(["iconutil", "-c", "icns", iconset,
         "-o", app / "Contents" / "Resources" / f"{APP_NAME}.icns"])
    shutil.rmtree(iconset, ignore_errors=True)


# ================================================================
# Bundle
# ================================================================

def write_plist(app: Path, build_id: str) -> None:
    info = {
        "CFBundleName": APP_NAME,
        "CFBundleDisplayName": APP_NAME,
        "CFBundleIdentifier": BUNDLE_ID,
        "CFBundleExecutable": APP_NAME,
        "CFBundleIconFile": f"{APP_NAME}.icns",
        "CFBundlePackageType": "APPL",
        "CFBundleShortVersionString": VERSION,
        "CFBundleVersion": build_id,
        "CFBundleInfoDictionaryVersion": "6.0",
        "LSMinimumSystemVersion": MIN_MACOS,
        "LSApplicationCategoryType": "public.app-category.music",
        "NSHighResolutionCapable": True,
        # The backend answers phones on the LAN and the P2P layer speaks DHT
        # and SSDP; macOS 15+ gates all of that behind one consent prompt
        # attributed to this bundle.
        "NSLocalNetworkUsageDescription":
            "Sautium serves its web player to your other devices and syncs "
            "with peers on your network.",
        # The Homebrew step offers to open Terminal for the user.
        "NSAppleEventsUsageDescription":
            "Sautium opens Terminal so you can paste the Homebrew install "
            "command.",
        # A FLAC library usually sits on an external drive or a NAS share, and
        # macOS asks before either is read.
        "NSRemovableVolumesUsageDescription":
            "Sautium reads the music library you point it at.",
        "NSNetworkVolumesUsageDescription":
            "Sautium reads the music library you point it at.",
        "NSDocumentsFolderUsageDescription":
            "Sautium reads the music library you point it at.",
        "NSDownloadsFolderUsageDescription":
            "Sautium reads the music library you point it at.",
    }
    with (app / "Contents" / "Info.plist").open("wb") as handle:
        plistlib.dump(info, handle)


def build_stub(app: Path, arch: str) -> None:
    run(["clang", "-O2", "-arch", arch, "-mmacosx-version-min=" + MIN_MACOS,
         "-o", app / "Contents" / "MacOS" / APP_NAME,
         Path(__file__).parent / "macos" / "launcher_stub.c"])


def stage_bootstrap(app: Path) -> None:
    shutil.copy2(Path(__file__).parent / "macos" / "bootstrap.py",
                 app / "Contents" / "Resources" / "bootstrap.py")


# ================================================================
# Signing
# ================================================================

def _macho_files(app: Path) -> list:
    """Every Mach-O inside the bundle, innermost first."""
    found = []
    for path in app.rglob("*"):
        if not path.is_file() or path.is_symlink():
            continue
        with path.open("rb") as handle:
            magic = handle.read(4)
        if magic in (b"\xcf\xfa\xed\xfe", b"\xce\xfa\xed\xfe", b"\xca\xfe\xba\xbe"):
            found.append(path)
    return sorted(found, key=lambda p: len(p.parts), reverse=True)


def sign(app: Path, identity: str) -> None:
    adhoc = identity == "-"
    options = [] if adhoc else ["--options", "runtime", "--timestamp"]
    # Nested code is signed individually rather than with --deep: --deep is
    # deprecated for distribution and silently skips things notarization then
    # rejects.
    for binary in _macho_files(app):
        run(["codesign", "--force", "--sign", identity, *options, binary],
            capture_output=True)
    run(["codesign", "--force", "--sign", identity, *options, app])
    run(["codesign", "--verify", "--strict", "--verbose=2", app])
    print(f"Signed with {'an ad-hoc signature' if adhoc else identity}")


# ================================================================
# DMG
# ================================================================

# Gatekeeper blocks an ad-hoc build before any of our own UI can explain
# itself, and the disk-image window is the only surface left to say it on.
FIRST_LAUNCH_NOTE = """Sautium — first launch on macOS
===============================

1. Drag Sautium onto the Applications folder in this window.

2. Open it. macOS will refuse the first time, saying it "cannot be opened
   because Apple cannot check it for malicious software" — this build is
   signed by its author rather than by Apple.

   Open System Settings -> Privacy & Security, scroll down, press
   "Open Anyway", and open Sautium again.

3. The first launch sets itself up (a few minutes). If Homebrew is missing
   it will ask for it: the command is copied for you — paste it into
   Terminal, let it finish, and press "Check again". PostgreSQL, ffmpeg and
   the rest arrive through it.

4. The setup wizard creates your account and the database. Its music
   catalogue step downloads ~21 GB in the background — untick it if you
   just want to look around.

5. In the launcher window: "Scan Library" points Sautium at your music
   folder, "Open Web UI" opens the player (accept the certificate warning
   once — the connection is to your own machine).

Sautium keeps everything in three folders. Deleting them and the app removes
it completely:

   ~/.local/share/Sautium    database, logs, the app's own Python
   ~/.config/Sautium         settings and your account key
   ~/.sautium                the certificate your browser trusted
"""


def make_dmg(app: Path, arch: str) -> Path:
    DIST_DIR.mkdir(parents=True, exist_ok=True)
    stage = BUILD_DIR / "dmg"
    shutil.rmtree(stage, ignore_errors=True)
    stage.mkdir(parents=True)
    shutil.copytree(app, stage / app.name, symlinks=True)
    (stage / "Applications").symlink_to("/Applications")
    (stage / "First launch.txt").write_text(FIRST_LAUNCH_NOTE, encoding="utf-8")

    dmg = DIST_DIR / f"{APP_NAME}-{VERSION}-{arch}.dmg"
    dmg.unlink(missing_ok=True)
    run(["hdiutil", "create", "-volname", APP_NAME, "-srcfolder", stage,
         "-fs", "HFS+", "-format", "UDZO", "-ov", dmg], capture_output=True)
    shutil.rmtree(stage, ignore_errors=True)
    return dmg


def notarize(dmg: Path, profile: str) -> None:
    run(["xcrun", "notarytool", "submit", dmg,
         "--keychain-profile", profile, "--wait"])
    run(["xcrun", "stapler", "staple", dmg])


# ================================================================

def main() -> None:
    parser = argparse.ArgumentParser(description="Build Sautium.app + DMG")
    parser.add_argument("--arch", choices=("arm64", "x86_64"),
                        default="arm64" if __import__("platform").machine() == "arm64" else "x86_64")
    parser.add_argument("--sign", default="-",
                        help="codesign identity; '-' (default) is ad-hoc")
    parser.add_argument("--notarize", metavar="KEYCHAIN_PROFILE",
                        help="notarize the DMG with `xcrun notarytool`")
    parser.add_argument("--skip-dmg", action="store_true")
    args = parser.parse_args()

    if sys.platform != "darwin":
        raise SystemExit("macOS only — use desktop/build.py for the Windows exe")
    if args.notarize and args.sign == "-":
        raise SystemExit("notarization needs a Developer ID identity (--sign)")

    app = BUILD_DIR / f"{APP_NAME}.app"
    shutil.rmtree(app, ignore_errors=True)
    (app / "Contents" / "MacOS").mkdir(parents=True)
    (app / "Contents" / "Resources").mkdir(parents=True)

    build_id = stage_payload(app)
    stage_runtime(app, args.arch)
    stage_bootstrap(app)
    stage_icon(app)
    write_plist(app, build_id)
    build_stub(app, args.arch)
    sign(app, args.sign)

    size = sum(f.stat().st_size for f in app.rglob("*") if f.is_file()) / 1e6
    print(f"\n{app}  ({size:.0f} MB)")

    if args.skip_dmg:
        return
    dmg = make_dmg(app, args.arch)
    if args.notarize:
        notarize(dmg, args.notarize)
    print(f"{dmg}  ({dmg.stat().st_size / 1e6:.0f} MB)")


if __name__ == "__main__":
    main()
