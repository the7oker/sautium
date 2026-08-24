"""
First-run bootstrapper for the macOS app bundle.

The bundle ships a private CPython (`Contents/Resources/runtime`) and a
snapshot of the tree (`Contents/Resources/payload`), and the app runs from
NEITHER. Both are installed into the data root the launcher already owns
(`~/.local/share/Sautium`), because the launcher writes inside its own tree —
`backend/mcp-windows.json`, `backend/data/.node_key`, the git updater — and an
app bundle is the wrong place for that: it is code-signed, lives in a shared
directory, and is replaced wholesale by the next DMG.

Running from a writable copy also keeps the launcher in the exact shape it was
written for: `python -m desktop`, `get_project_root()` = that copy,
`get_backend_python()` = `sys.executable` = the venv beside it. Nothing in the
launcher needs to know it was started from a bundle.

Stdlib only — the venv this builds is where third-party code starts.
"""

import hashlib
import os
import queue
import shutil
import subprocess
import sys
import threading
import traceback
from pathlib import Path
from typing import Callable, Optional

RESOURCES = Path(__file__).resolve().parent
BUNDLED_RUNTIME = RESOURCES / "runtime"
PAYLOAD = RESOURCES / "payload"

BREW_INSTALL_CMD = (
    '/bin/bash -c "$(curl -fsSL '
    'https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"'
)

# backend/static/tokens.css — the app's first screen should already look like
# the app.
BG = "#1B1714"
SURFACE = "#2A2420"
DIVIDER = "#3A322C"
TEXT = "#EDE2D4"
MUTED = "#A69B8E"
DIM = "#6E665C"
AMBER = "#E8B06F"
AMBER_PRESS = "#D29A5B"
RED = "#C1564E"

UI_FONT = "Helvetica Neue"


def data_root() -> Path:
    """Mirrors desktop.config_manager.get_data_dir(): one root for config,
    pgdata, logs and the installed tree."""
    base = os.environ.get("XDG_DATA_HOME")
    return (Path(base) if base else Path.home() / ".local" / "share") / "Sautium"


ROOT = data_root()
APP_DIR = ROOT / "app"
# The interpreter is COPIED out of the bundle rather than run from it. A venv
# keeps its stdlib in the base prefix, so a venv built against the bundle would
# have the launcher, the backend and every child process importing — and
# bytecode-caching into — a code-signed app that may sit on read-only media.
RUNTIME_DIR = ROOT / "runtime"
RUNTIME_PYTHON = RUNTIME_DIR / "bin" / "python3"
RUNTIME_STAMP = "runtime.version"
VENV_DIR = ROOT / "venv"
VENV_PYTHON = VENV_DIR / "bin" / "python"
DEPS_MARKER = ROOT / ".launcher_deps_hash"
LOG_PATH = ROOT / "bootstrap.log"


def log(line: str) -> None:
    ROOT.mkdir(parents=True, exist_ok=True)
    with LOG_PATH.open("a", encoding="utf-8") as fh:
        fh.write(line.rstrip("\n") + "\n")


def find_brew() -> Optional[str]:
    """Well-known paths first: an app launched from Finder inherits a minimal
    PATH that has never heard of /opt/homebrew."""
    for candidate in ("/opt/homebrew/bin/brew", "/usr/local/bin/brew"):
        if os.path.isfile(candidate):
            return candidate
    return shutil.which("brew")


def bundle_build_id() -> str:
    return (PAYLOAD / ".sautium_build").read_text(encoding="utf-8").strip()


def installed_build_id() -> str:
    marker = APP_DIR / ".sautium_build"
    if not marker.exists():
        return ""
    return marker.read_text(encoding="utf-8").strip()


def requirements_hash() -> str:
    req = APP_DIR / "desktop" / "requirements.txt"
    return hashlib.sha256(req.read_bytes()).hexdigest()


def runtime_current() -> bool:
    installed = RUNTIME_DIR / RUNTIME_STAMP
    if not RUNTIME_PYTHON.exists() or not installed.exists():
        return False
    return installed.read_text().strip() == (BUNDLED_RUNTIME / RUNTIME_STAMP).read_text().strip()


def is_installed() -> bool:
    """True when this exact bundle is already unpacked with its deps in place."""
    if not VENV_PYTHON.exists() or not runtime_current():
        return False
    if installed_build_id() != bundle_build_id():
        return False
    return DEPS_MARKER.exists() and DEPS_MARKER.read_text().strip() == requirements_hash()


def run_streamed(cmd: list, report: Callable[[str], None]) -> None:
    """Run a subprocess, mirroring its output into the log and the UI."""
    log(f"$ {' '.join(cmd)}")
    env = dict(os.environ)
    env["PYTHONNOUSERSITE"] = "1"
    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, bufsize=1, env=env,
    )
    for line in proc.stdout:
        line = line.rstrip()
        if line:
            log(line)
            report(line)
    if proc.wait() != 0:
        raise RuntimeError(f"{Path(cmd[0]).name} failed (exit {proc.returncode}) — see {LOG_PATH}")


def sync_payload(report: Callable[[str], None]) -> None:
    report("Installing application files…")
    # backend/data holds the peer surface's own node key — code is replaced on
    # every update, that identity is not.
    keep = APP_DIR / "backend" / "data"
    stash = ROOT / ".backend_data_stash"
    if keep.exists():
        shutil.rmtree(stash, ignore_errors=True)
        shutil.move(str(keep), str(stash))
    if APP_DIR.exists():
        shutil.rmtree(APP_DIR)
    shutil.copytree(PAYLOAD, APP_DIR, symlinks=True)
    if stash.exists():
        shutil.rmtree(keep, ignore_errors=True)
        shutil.move(str(stash), str(keep))
    log(f"payload installed: {bundle_build_id()}")


def install_runtime(report: Callable[[str], None]) -> None:
    if runtime_current():
        return
    report("Installing the Python runtime…")
    # A new interpreter invalidates the wheels compiled against the old one.
    shutil.rmtree(VENV_DIR, ignore_errors=True)
    DEPS_MARKER.unlink(missing_ok=True)
    shutil.rmtree(RUNTIME_DIR, ignore_errors=True)
    shutil.copytree(BUNDLED_RUNTIME, RUNTIME_DIR, symlinks=True)
    log(f"runtime installed: {(RUNTIME_DIR / RUNTIME_STAMP).read_text().strip()}")


def ensure_venv(report: Callable[[str], None]) -> None:
    if VENV_PYTHON.exists():
        return
    report("Creating the Python environment…")
    shutil.rmtree(VENV_DIR, ignore_errors=True)
    # --copies, not symlinks: nothing here may break when the .app is replaced.
    run_streamed([str(RUNTIME_PYTHON), "-m", "venv", "--copies", str(VENV_DIR)], report)


def install_deps(report: Callable[[str], None]) -> None:
    if DEPS_MARKER.exists() and DEPS_MARKER.read_text().strip() == requirements_hash():
        return
    report("Installing launcher dependencies…")
    run_streamed(
        [str(VENV_PYTHON), "-m", "pip", "install", "--disable-pip-version-check",
         "-r", str(APP_DIR / "desktop" / "requirements.txt")],
        report,
    )
    DEPS_MARKER.write_text(requirements_hash())


def launch() -> None:
    """Replace this process with the launcher. Same PID, so LaunchServices
    keeps the Dock icon, the app name and the activation state."""
    os.chdir(APP_DIR)
    env = dict(os.environ)
    env["PYTHONNOUSERSITE"] = "1"
    log(f"exec {VENV_PYTHON} -m desktop")
    os.execve(str(VENV_PYTHON), [str(VENV_PYTHON), "-m", "desktop"], env)


# ================================================================
# UI
# ================================================================

import tkinter as tk   # noqa: E402  (kept below the headless helpers on purpose)


def _label(parent, text, *, color=TEXT, size=13, weight="normal", **kw):
    return tk.Label(parent, text=text, bg=kw.pop("bg", BG), fg=color,
                    font=(UI_FONT, size, weight), **kw)


def _button(parent, text, command, *, primary=False):
    """tk.Button ignores background colour under Aqua, so the buttons are
    labels that know how to be pressed."""
    bg = AMBER if primary else SURFACE
    fg = BG if primary else TEXT
    frame = tk.Frame(parent, bg=bg, highlightthickness=0 if primary else 1,
                     highlightbackground=DIVIDER)
    label = tk.Label(frame, text=text, bg=bg, fg=fg, font=(UI_FONT, 12),
                     padx=16, pady=7, cursor="pointinghand")
    label.pack()

    def press(_):
        pressed = AMBER_PRESS if primary else DIVIDER
        frame.configure(bg=pressed)
        label.configure(bg=pressed)

    def release(_):
        frame.configure(bg=bg)
        label.configure(bg=bg)
        command()

    for widget in (frame, label):
        widget.bind("<Button-1>", press)
        widget.bind("<ButtonRelease-1>", release)
    return frame


class ProgressBar(tk.Canvas):
    """Indeterminate sweep — pip gives no usable percentage and a fake one
    would be a lie."""

    WIDTH, HEIGHT, BLOCK = 480, 4, 140

    def __init__(self, parent):
        super().__init__(parent, width=self.WIDTH, height=self.HEIGHT,
                         bg=DIVIDER, highlightthickness=0)
        self._block = self.create_rectangle(0, 0, self.BLOCK, self.HEIGHT,
                                            fill=AMBER, width=0)
        self._x = -self.BLOCK
        self._running = False

    def start(self):
        if not self._running:
            self._running = True
            self._step()

    def stop(self):
        self._running = False

    def _step(self):
        if not self._running:
            return
        self._x += 6
        if self._x > self.WIDTH:
            self._x = -self.BLOCK
        self.coords(self._block, self._x, 0, self._x + self.BLOCK, self.HEIGHT)
        self.after(16, self._step)


class BootstrapWindow:
    def __init__(self):
        self.root = tk.Tk()
        self.root.title("Sautium")
        self.root.configure(bg=BG)
        self.root.geometry("560x420")
        self.root.resizable(False, False)
        self.events: queue.Queue = queue.Queue()

        self.body = tk.Frame(self.root, bg=BG)
        self.body.pack(fill="both", expand=True, padx=32, pady=28)

        _label(self.body, "Sautium.", color=AMBER, size=30, weight="bold").pack(anchor="w")
        self.subtitle = _label(self.body, "", color=MUTED, size=13)
        self.subtitle.pack(anchor="w", pady=(2, 0))

        self.content = tk.Frame(self.body, bg=BG)
        self.content.pack(fill="both", expand=True, pady=(24, 0))

        self.root.after(80, self._drain)

    # ---- plumbing ----

    def post(self, kind: str, payload=None):
        self.events.put((kind, payload))

    def _drain(self):
        while True:
            try:
                kind, payload = self.events.get_nowait()
            except queue.Empty:
                break
            handler = getattr(self, f"_on_{kind}", None)
            if handler:
                handler(payload)
        self.root.after(80, self._drain)

    def _clear(self):
        for child in self.content.winfo_children():
            child.destroy()

    # ---- screens ----

    def show_progress(self, subtitle: str):
        self.subtitle.configure(text=subtitle)
        self._clear()
        self.status = _label(self.content, "Starting…", size=14)
        self.status.pack(anchor="w")
        self.progress = ProgressBar(self.content)
        self.progress.pack(anchor="w", pady=(14, 18))
        self.progress.start()
        self.detail = tk.Text(self.content, height=9, bg=SURFACE, fg=DIM,
                              font=("Menlo", 10), relief="flat", wrap="none",
                              highlightthickness=0, padx=12, pady=10)
        self.detail.pack(fill="both", expand=True)
        self.detail.configure(state="disabled")

    def _on_status(self, text: str):
        self.status.configure(text=text)

    def _on_detail(self, text: str):
        self.detail.configure(state="normal")
        self.detail.insert("end", text + "\n")
        self.detail.see("end")
        self.detail.configure(state="disabled")

    def _on_done(self, _):
        launch()

    def _on_error(self, message: str):
        self.progress.stop()
        self.subtitle.configure(text="Setup failed")
        self._clear()
        _label(self.content, "Something went wrong", color=RED, size=16,
               weight="bold").pack(anchor="w")
        _label(self.content, message, color=MUTED, size=12, wraplength=480,
               justify="left").pack(anchor="w", pady=(8, 0))
        _label(self.content, str(LOG_PATH), color=DIM, size=11).pack(anchor="w", pady=(12, 0))
        row = tk.Frame(self.content, bg=BG)
        row.pack(anchor="w", pady=(20, 0))
        _button(row, "Open log", lambda: subprocess.run(["open", "-t", str(LOG_PATH)])
                ).pack(side="left")
        _button(row, "Quit", self.root.destroy, primary=True).pack(side="left", padx=(10, 0))

    def _on_needs_brew(self, _):
        self.progress.stop()
        self.subtitle.configure(text="One thing is missing")
        self._clear()
        _label(self.content, "Homebrew is required", size=16, weight="bold").pack(anchor="w")
        _label(
            self.content,
            "Sautium runs its own PostgreSQL 18 and uses ffmpeg for audio "
            "analysis. Both are installed through Homebrew, the standard macOS "
            "package manager. Paste this line into Terminal, follow its "
            "prompts, then come back and press Check again.",
            color=MUTED, size=12, wraplength=480, justify="left",
        ).pack(anchor="w", pady=(8, 14))

        command = tk.Entry(self.content, bg=SURFACE, fg=TEXT, font=("Menlo", 10),
                           relief="flat", highlightthickness=1,
                           highlightbackground=DIVIDER, insertbackground=TEXT)
        command.insert(0, BREW_INSTALL_CMD)
        command.configure(state="readonly", readonlybackground=SURFACE)
        command.pack(fill="x", ipady=8)

        row = tk.Frame(self.content, bg=BG)
        row.pack(anchor="w", pady=(18, 0))
        _button(row, "Copy command", self._copy_brew_command).pack(side="left")
        _button(row, "Open Terminal", self._open_terminal).pack(side="left", padx=(10, 0))
        _button(row, "Check again", self._recheck_brew, primary=True).pack(side="left", padx=(10, 0))

    def _copy_brew_command(self):
        subprocess.run(["pbcopy"], input=BREW_INSTALL_CMD, text=True)

    def _open_terminal(self):
        self._copy_brew_command()
        subprocess.run(["open", "-a", "Terminal"])

    def _recheck_brew(self):
        if find_brew():
            self.show_progress("Setting up — this happens once.")
            self.post("status", "Continuing…")
            start_worker(self)
        else:
            self.post("status", "Still not found — is the Terminal step finished?")

    def _on_volume_warning(self, _):
        self.subtitle.configure(text="Almost there")
        self._clear()
        _label(self.content, "Move Sautium to Applications", size=16,
               weight="bold").pack(anchor="w")
        _label(
            self.content,
            "Sautium is running from the disk image. Drag it onto the "
            "Applications folder in the window behind this one, eject the "
            "image, and open it from there.",
            color=MUTED, size=12, wraplength=480, justify="left",
        ).pack(anchor="w", pady=(8, 0))
        row = tk.Frame(self.content, bg=BG)
        row.pack(anchor="w", pady=(20, 0))
        _button(row, "Run from here anyway", self._ignore_volume).pack(side="left")
        _button(row, "Quit", self.root.destroy, primary=True).pack(side="left", padx=(10, 0))

    def _ignore_volume(self):
        self.show_progress("Setting up — this happens once.")
        start_worker(self)


def start_worker(window: BootstrapWindow) -> None:
    def work():
        try:
            if not find_brew():
                window.post("needs_brew", None)
                return
            sync_payload(lambda text: window.post("status", text))
            install_runtime(lambda text: window.post("status", text))
            ensure_venv(lambda line: window.post("detail", line))
            window.post("status", "Creating the Python environment…")
            install_deps(lambda line: window.post("detail", line))
            window.post("status", "Starting Sautium…")
            window.post("done", None)
        except Exception as exc:
            log(traceback.format_exc())
            window.post("error", str(exc))

    threading.Thread(target=work, daemon=True, name="bootstrap").start()


def main() -> None:
    if not PAYLOAD.exists() or not BUNDLED_RUNTIME.exists():
        sys.exit("bootstrap.py runs from inside Sautium.app, not on its own")

    if is_installed() and find_brew():
        launch()   # never returns

    log(f"--- bootstrap {bundle_build_id()} ---")
    window = BootstrapWindow()
    if str(RESOURCES).startswith("/Volumes/"):
        window.subtitle.configure(text="")
        window.post("volume_warning", None)
    else:
        window.show_progress("Setting up — this happens once.")
        start_worker(window)
    window.root.mainloop()


if __name__ == "__main__":
    main()
