r"""
First-run bootstrapper for the Windows install.

Setup puts three things in %LOCALAPPDATA%\Programs\Sautium — a private CPython
(`runtime\`), a MinGit (`git\`) and a snapshot of the tree (`payload\`) — and
Sautium runs from only the first. The tree is cloned into the launcher's data
root (%LOCALAPPDATA%\Sautium\app), because the launcher writes inside its own
tree — `backend\mcp-windows.json`, the node key under `backend\data`, the
PostgreSQL, Python and audio tools it downloads beside itself, the git updater
— and the installed folder is replaced wholesale by the next Setup.exe.

The runtime is used where it lies: a per-user install is the user's own
folder, so pip may put the launcher's packages into it, and Setup wipes and
re-lays it on upgrade (the dependency marker lives inside it for that reason).
Running the tree from a writable copy keeps the launcher in the exact shape it
was written for — `python -m desktop`, `get_project_root()` = the clone —
with everything under it provisioned by the launcher on first start, as on the
maintainer's own checkout. Nothing in the launcher needs to know it was
started from an installer.

Stdlib only — the runtime this prepares is where third-party code starts.
"""

import ctypes
import hashlib
import os
import queue
import shutil
import stat
import subprocess
import sys
import threading
import traceback
from pathlib import Path
from typing import Callable

HERE = Path(__file__).resolve().parent
RUNTIME_DIR = HERE / "runtime"
RUNTIME_PYTHON = RUNTIME_DIR / "python.exe"     # pip runs on the console interpreter
LAUNCHER_EXE = RUNTIME_DIR / "Sautium.exe"      # the runtime's pythonw under the app's name
RUNTIME_STAMP = RUNTIME_DIR / "runtime.version"
# Inside the runtime on purpose: an upgrade replaces the runtime folder whole
# (sautium.iss [InstallDelete]), and the packages the marker vouches for go
# with it.
DEPS_MARKER = RUNTIME_DIR / ".launcher_deps"
GIT_DIR = HERE / "git"
GIT = GIT_DIR / "cmd" / "git.exe"
PAYLOAD = HERE / "payload"
ICON = HERE / "Sautium.ico"

# The tree is a git checkout, not a copy: the launcher already knows how to
# update one — fetch, reset to the tip, run new migrations, restart the
# backend — and that path is exercised daily on the maintainer's own machine.
# The bundled payload seeds an install that cannot reach GitHub; once a clone
# exists, git owns the directory and Setup stops touching it.
REPO_URL = "https://github.com/the7oker/sautium.git"
REPO_BRANCH = "main"

# Shared with the launcher (desktop/utils.py) and the installer
# (desktop/installer/sautium.iss): the shortcut's AppUserModelID groups its
# taskbar button with the running window, and AppMutex is what Setup and
# Uninstall wait on before touching the runtime.
APP_USER_MODEL_ID = "Sautium.Launcher"
APP_MUTEX = "SautiumLauncher"

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

UI_FONT = "Segoe UI"
MONO_FONT = "Consolas"


def data_root() -> Path:
    """Mirrors desktop.config_manager.get_data_dir(): one root for pgdata,
    logs, downloaded components and the installed tree."""
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(base) / "Sautium"


ROOT = data_root()
APP_DIR = ROOT / "app"
LOG_PATH = ROOT / "bootstrap.log"

_mutex = None


def log(line: str) -> None:
    ROOT.mkdir(parents=True, exist_ok=True)
    with LOG_PATH.open("a", encoding="utf-8") as fh:
        fh.write(line.rstrip("\n") + "\n")


def claim_app_identity() -> None:
    global _mutex
    ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_USER_MODEL_ID)
    _mutex = ctypes.windll.kernel32.CreateMutexW(None, False, APP_MUTEX)


def git_env() -> dict:
    """The bundled git, deaf to the machine: no system config (MinGit's own
    would switch on autocrlf and the credential manager, and pull in a Git
    for Windows installed beside it), and no prompts — a repository that
    stopped being public must fail fast, not hang a window nobody can type
    into."""
    env = dict(os.environ)
    env["PATH"] = str(GIT_DIR / "cmd") + os.pathsep + env.get("PATH", "")
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GCM_INTERACTIVE"] = "never"
    return env


def launcher_env() -> dict:
    env = git_env()
    # Our interpreter, nobody else's packages: a Python the user installed for
    # themselves may leave a user site-packages, PYTHONPATH or PYTHONHOME
    # behind, and any of them would reach into this one.
    env["PYTHONNOUSERSITE"] = "1"
    env.pop("PYTHONPATH", None)
    env.pop("PYTHONHOME", None)
    # The launcher relaunches itself through here after an update, so a
    # changed desktop/requirements.txt is installed before the new code runs.
    env["SAUTIUM_BOOTSTRAP"] = str(Path(__file__).resolve())
    return env


def bundle_build_id() -> str:
    return (PAYLOAD / ".sautium_build").read_text(encoding="utf-8").strip()


def installed_build_id() -> str:
    marker = APP_DIR / ".sautium_build"
    if not marker.exists():
        return ""
    return marker.read_text(encoding="utf-8").strip()


def tree_is_clone() -> bool:
    """A repository that got its files. `.git` alone is a checkout that was
    interrupted between fetch and checkout — re-done, not trusted."""
    return (APP_DIR / ".git").is_dir() and (APP_DIR / "desktop" / "requirements.txt").exists()


def tree_current() -> bool:
    """A clone is never "out of date" as far as Setup is concerned — the
    launcher's own update path owns it from then on, and a newer Setup must
    not silently roll it back to the snapshot it happens to carry."""
    return tree_is_clone() or installed_build_id() == bundle_build_id()


def deps_key() -> str:
    """What the marker must say: this runtime, these requirements."""
    requirements = APP_DIR / "desktop" / "requirements.txt"
    return (f"{RUNTIME_STAMP.read_text(encoding='utf-8').strip()} "
            f"{hashlib.sha256(requirements.read_bytes()).hexdigest()}")


def deps_current() -> bool:
    return DEPS_MARKER.exists() and DEPS_MARKER.read_text(encoding="utf-8").strip() == deps_key()


def is_installed() -> bool:
    return tree_current() and deps_current()


def remove_tree(path: Path) -> None:
    """rmtree that gets past git's read-only objects — ignore_errors would
    leave a half-deleted .git behind and the next start would take it for a
    clone."""
    def unlock(func, target, _exc):
        os.chmod(target, stat.S_IWRITE)
        func(target)
    if path.exists():
        shutil.rmtree(path, onexc=unlock)


def run_streamed(cmd: list, report: Callable[[str], None], env: dict) -> None:
    """Run a subprocess, mirroring its output into the log and the UI."""
    cmd = [str(part) for part in cmd]
    log(f"$ {' '.join(cmd)}")
    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace", bufsize=1, env=env,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    for line in proc.stdout:
        line = line.rstrip()
        if line:
            log(line)
            report(line)
    if proc.wait() != 0:
        raise RuntimeError(f"{Path(cmd[0]).name} failed (exit {proc.returncode}) — see {LOG_PATH}")


def sync_payload(report: Callable[[str], None]) -> None:
    """Put the tree in place — as a clone when GitHub is reachable, from the
    bundle when it is not. A clone is left alone: after the first one, updates
    are the launcher's job, not Setup's."""
    if tree_is_clone():
        log("tree is a clone — left to the launcher's updater")
        return
    if not clone_tree(report):
        copy_payload(report)


def clone_tree(report: Callable[[str], None]) -> bool:
    """Turn APP_DIR into a checkout of main, in place. Not `git clone`: the
    folder is rarely empty — an earlier start that could not reach GitHub
    left the bundled copy here, and the launcher then filled it with the
    PostgreSQL, Python and audio tools it downloads beside the tree, which a
    fresh clone into an empty folder would throw away. init + fetch +
    checkout overwrites the tracked files and touches nothing else.

    Not fatal: an install with no network runs from the bundled copy, and the
    next start that can reach GitHub replaces it with a clone."""
    report("Downloading the latest version…")
    APP_DIR.mkdir(parents=True, exist_ok=True)
    env = git_env()
    try:
        # config rather than `remote add`: re-runnable over a repository an
        # interrupted attempt left behind. The refspec is what makes the
        # fetch update origin/main, which the checkout and the launcher's
        # updater both read.
        run_streamed([GIT, "-C", APP_DIR, "init", "--quiet"], report, env)
        run_streamed([GIT, "-C", APP_DIR, "config", "remote.origin.url", REPO_URL], report, env)
        run_streamed([GIT, "-C", APP_DIR, "config", "remote.origin.fetch",
                      "+refs/heads/*:refs/remotes/origin/*"], report, env)
        run_streamed([GIT, "-C", APP_DIR, "fetch", "--progress", "origin", REPO_BRANCH], report, env)
        run_streamed([GIT, "-C", APP_DIR, "checkout", "--force", "-B", REPO_BRANCH,
                      f"origin/{REPO_BRANCH}"], report, env)
    except Exception as exc:
        log(f"clone failed ({exc}) — falling back to the bundled copy")
        remove_tree(APP_DIR / ".git")
        return False
    # The copy's stamp would tell the launcher it is a packaged tree that
    # cannot pull.
    (APP_DIR / ".sautium_build").unlink(missing_ok=True)
    log(f"cloned {REPO_URL} ({REPO_BRANCH})")
    return True


def copy_payload(report: Callable[[str], None]) -> None:
    """The bundled snapshot over the tracked roots — and only those: what the
    launcher downloaded beside them stays. backend\\data holds the peer
    surface's own node key; code is replaced on every update, that identity
    is not."""
    report("Installing application files…")
    keep = APP_DIR / "backend" / "data"
    stash = ROOT / ".backend_data_stash"
    if keep.exists():
        remove_tree(stash)
        shutil.move(str(keep), str(stash))
    for entry in PAYLOAD.iterdir():
        target = APP_DIR / entry.name
        if entry.is_dir():
            remove_tree(target)
            shutil.copytree(entry, target)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(entry, target)
    if stash.exists():
        remove_tree(keep)
        shutil.move(str(stash), str(keep))
    log(f"payload copied from the bundle: {bundle_build_id()}")


def install_deps(report: Callable[[str], None]) -> None:
    if deps_current():
        return
    report("Installing launcher dependencies…")
    run_streamed(
        [RUNTIME_PYTHON, "-m", "pip", "install", "--disable-pip-version-check",
         "--only-binary=:all:", "-r", APP_DIR / "desktop" / "requirements.txt"],
        report, launcher_env(),
    )
    DEPS_MARKER.write_text(deps_key(), encoding="utf-8")


def launch() -> None:
    """Start the launcher and let this process go. No exec on Windows — the
    shortcut and Explorer never wait on us, so a child is as good as a
    replacement, and it is the one whose name Task Manager shows."""
    log(f"start {LAUNCHER_EXE} -m desktop")
    subprocess.Popen([str(LAUNCHER_EXE), "-m", "desktop"], cwd=str(APP_DIR),
                     env=launcher_env())


# ================================================================
# UI
# ================================================================

import tkinter as tk   # noqa: E402  (kept below the headless helpers on purpose)


def _label(parent, text, *, color=TEXT, size=11, weight="normal", **kw):
    return tk.Label(parent, text=text, bg=kw.pop("bg", BG), fg=color,
                    font=(UI_FONT, size, weight), **kw)


class BootstrapWindow:
    """Same screen as the macOS bundle's, drawn in device pixels: Tk on Windows
    is not DPI-aware unless the process says so, and the alternative is a
    blurred window on every high-density laptop."""

    def __init__(self):
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except (AttributeError, OSError):
            ctypes.windll.user32.SetProcessDPIAware()
        self.root = tk.Tk()
        self.root.title("Sautium")
        self.root.iconbitmap(default=str(ICON))
        self.root.configure(bg=BG)
        self.scale = self.root.winfo_fpixels("1i") / 96
        self.root.geometry(f"{self.px(560)}x{self.px(420)}")
        self.root.resizable(False, False)
        self.events: queue.Queue = queue.Queue()

        self.body = tk.Frame(self.root, bg=BG)
        self.body.pack(fill="both", expand=True, padx=self.px(32), pady=self.px(28))

        _label(self.body, "Sautium.", color=AMBER, size=24, weight="bold").pack(anchor="w")
        self.subtitle = _label(self.body, "", color=MUTED, size=11)
        self.subtitle.pack(anchor="w", pady=(self.px(2), 0))

        self.content = tk.Frame(self.body, bg=BG)
        self.content.pack(fill="both", expand=True, pady=(self.px(24), 0))

        self.root.after(80, self._drain)

    def px(self, n: int) -> int:
        return round(n * self.scale)

    def _button(self, parent, text, command, *, primary=False):
        bg = AMBER if primary else SURFACE
        fg = BG if primary else TEXT
        frame = tk.Frame(parent, bg=bg, highlightthickness=0 if primary else 1,
                         highlightbackground=DIVIDER)
        label = tk.Label(frame, text=text, bg=bg, fg=fg, font=(UI_FONT, 10),
                         padx=self.px(16), pady=self.px(7), cursor="hand2")
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
        self.status = _label(self.content, "Starting…", size=12)
        self.status.pack(anchor="w")
        self.progress = ProgressBar(self.content, self.px)
        self.progress.pack(anchor="w", pady=(self.px(14), self.px(18)))
        self.progress.start()
        self.detail = tk.Text(self.content, height=9, bg=SURFACE, fg=DIM,
                              font=(MONO_FONT, 9), relief="flat", wrap="none",
                              highlightthickness=0, padx=self.px(12), pady=self.px(10))
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
        self.root.destroy()

    def _on_error(self, message: str):
        self.progress.stop()
        self.subtitle.configure(text="Setup failed")
        self._clear()
        _label(self.content, "Something went wrong", color=RED, size=14,
               weight="bold").pack(anchor="w")
        _label(self.content, message, color=MUTED, size=10, wraplength=self.px(480),
               justify="left").pack(anchor="w", pady=(self.px(8), 0))
        _label(self.content, str(LOG_PATH), color=DIM, size=9).pack(anchor="w", pady=(self.px(12), 0))
        row = tk.Frame(self.content, bg=BG)
        row.pack(anchor="w", pady=(self.px(20), 0))
        self._button(row, "Open log", lambda: os.startfile(str(LOG_PATH))).pack(side="left")
        self._button(row, "Quit", self.root.destroy, primary=True).pack(
            side="left", padx=(self.px(10), 0))


class ProgressBar(tk.Canvas):
    """Indeterminate sweep — pip gives no usable percentage and a fake one
    would be a lie."""

    def __init__(self, parent, px):
        self.width, self.height, self.block = px(480), px(4), px(140)
        super().__init__(parent, width=self.width, height=self.height,
                         bg=DIVIDER, highlightthickness=0)
        self._block = self.create_rectangle(0, 0, self.block, self.height,
                                            fill=AMBER, width=0)
        self._x = -self.block
        self._step_px = px(6)
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
        self._x += self._step_px
        if self._x > self.width:
            self._x = -self.block
        self.coords(self._block, self._x, 0, self._x + self.block, self.height)
        self.after(16, self._step)


def start_worker(window: BootstrapWindow) -> None:
    def work():
        try:
            sync_payload(lambda text: window.post("status", text))
            install_deps(lambda line: window.post("detail", line))
            window.post("status", "Starting Sautium…")
            window.post("done", None)
        except Exception as exc:
            log(traceback.format_exc())
            window.post("error", str(exc))

    threading.Thread(target=work, daemon=True, name="bootstrap").start()


def main() -> None:
    if not PAYLOAD.exists() or not RUNTIME_PYTHON.exists():
        sys.exit("bootstrap.py runs from the installed Sautium folder, not on its own")

    claim_app_identity()
    try:
        installed = is_installed()
    except OSError as exc:
        # A half-written tree or runtime: there is no window yet to say so,
        # so the setup screen is where it gets said.
        log(f"state check failed ({exc}) — running setup")
        installed = False
    if installed:
        launch()
        return

    log(f"--- bootstrap {bundle_build_id()} ---")
    window = BootstrapWindow()
    window.show_progress("Setting up — this happens once.")
    start_worker(window)
    window.root.mainloop()


if __name__ == "__main__":
    main()
