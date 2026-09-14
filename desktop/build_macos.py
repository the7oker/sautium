"""
Build Sautium.app and its DMG.

    python desktop/build_macos.py               # host arch, ad-hoc signature
    python desktop/build_macos.py --arch x86_64
    python desktop/build_macos.py --sign "Developer ID Application: …" \
                                 --notarize <keychain-profile>

The bundle is NOT a frozen launcher. It carries a private CPython plus a
snapshot of the tree, and `Contents/Resources/bootstrap.py` installs both into
the launcher's data root on first run — see that file for why, and
build_common.py for the pieces the Windows installer shares with this.
"""

import argparse
import plistlib
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from desktop.build_common import (  # noqa: E402
    APP_NAME, BUILD_DIR, DIST_DIR, VERSION, run, stage_payload, unpack_runtime,
)
from desktop.icon import render_icon  # noqa: E402

BUILD = BUILD_DIR / "macos"
BUNDLE_ID = "net.sautium.launcher"
MIN_MACOS = "12.0"

RUNTIME_PRUNE = ("lib/python3.12/idlelib", "lib/python3.12/turtledemo",
                 "lib/python3.12/test", "share/man")


# ================================================================
# Bundle contents
# ================================================================

def stage_runtime(app: Path, arch: str) -> None:
    target = "aarch64-apple-darwin" if arch == "arm64" else "x86_64-apple-darwin"
    unpack_runtime(target, app / "Contents" / "Resources" / "runtime", RUNTIME_PRUNE)


def stage_icon(app: Path) -> None:
    iconset = BUILD / f"{APP_NAME}.iconset"
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
    stage = BUILD / "dmg"
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
        raise SystemExit("macOS only — use desktop/build_windows.py for the Windows installer")
    if args.notarize and args.sign == "-":
        raise SystemExit("notarization needs a Developer ID identity (--sign)")

    app = BUILD / f"{APP_NAME}.app"
    shutil.rmtree(app, ignore_errors=True)
    (app / "Contents" / "MacOS").mkdir(parents=True)
    (app / "Contents" / "Resources").mkdir(parents=True)

    build_id = stage_payload(app / "Contents" / "Resources" / "payload")
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
