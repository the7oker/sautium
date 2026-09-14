"""Run the backup CLI from the launcher — never a second implementation.

The launcher owns no backup, export or import code of its own: Settings &
Tools › Backup & Restore runs `python -m backup create|export|import`
(backend/backup.py) — the same CLI a Docker node's weekly task runs — on the
backend interpreter with the backend's own environment
(service_manager.backend_env → backend.env: the DSN, the identity dir,
BACKUP_DIR=<data_dir>/backup, PG_BIN=pgsql/bin), and reads its progress as
JSON lines. The account password travels in the child's environment
(`--password-env`), never on a command line where a process list would show
it; cancel is a line on the child's stdin (`--cancel-on-stdin`), and the end
of that pipe — the launcher going away — ends the job too, so a quit
mid-backup leaves no half-written file.
"""

import json
import logging
import subprocess
import sys
import threading
from pathlib import Path
from typing import Callable, Optional

from desktop import node_backup as nb

logger = logging.getLogger(__name__)

PASSWORD_ENV = "SAUTIUM_BACKUP_PASSWORD"


def backup_dir() -> Path:
    from desktop.config_manager import get_data_dir
    return get_data_dir() / "backup"


def export_dir() -> Path:
    from desktop.config_manager import get_data_dir
    return get_data_dir() / "export"


def latest_backup(directory: Optional[Path] = None) -> Optional[dict]:
    """Header facts of the newest .sbk in the launcher's backup dir, or None."""
    d = directory or backup_dir()
    if not d.is_dir():
        return None
    files = sorted(d.glob("*" + nb.FILE_SUFFIX), key=lambda p: p.stat().st_mtime)
    if not files:
        return None
    path = files[-1]
    entry = {"name": path.name, "size": path.stat().st_size, "path": path}
    try:
        with open(path, "rb") as fp:
            header, _ = nb.read_header(fp)
        entry.update(created_at=header.get("created_at"), username=header["node"]["username"])
    except (nb.BackupError, OSError) as e:
        entry["error"] = str(e)
    return entry


def open_folder(path: Path) -> None:
    """The host's file manager on `path` — where the files land."""
    import os
    import subprocess
    path.mkdir(parents=True, exist_ok=True)
    if sys.platform == "win32":
        os.startfile(str(path))                      # type: ignore[attr-defined]
    elif sys.platform == "darwin":
        subprocess.Popen(["open", str(path)])
    else:
        subprocess.Popen(["xdg-open", str(path)])


def fmt_bytes(n: int) -> str:
    if n >= 1024 ** 3:
        return f"{n / 1024 ** 3:.1f} GB"
    return f"{n / 1024 ** 2:.0f} MB"


def describe_event(ev: dict, job: str = "Backup") -> str:
    """One line of launcher UI per CLI event; `job` names the task."""
    phase = ev.get("phase")
    if phase == "counting":
        return f"{job}: taking a snapshot and counting rows…"
    if phase == "dumping":
        return f"{job}: {fmt_bytes(int(ev.get('bytes') or 0))} written…"
    if phase == "paused":
        return f"{job} paused while music plays — resumes when playback stops"
    if phase == "identity":
        return f"{job}: adding identity documents…"
    if phase == "scope":
        return (f"{job}: {ev.get('albums', 0):,} albums, {ev.get('tracks', 0):,} tracks…"
                if ev.get("albums") is not None else f"{job}: selecting…")
    if phase in ("writing", "importing"):
        return f"{job}: {ev.get('tracks_done', 0):,} / {ev.get('tracks', 0):,} tracks…"
    if phase == "verifying":
        return f"{job}: verifying the file…"
    if phase == "plan" and "scopes" in ev:
        sc = ev["scopes"]
        return (f"{job}: {sc.get('analysed', 0):,} albums with analysis here, "
                f"{sc.get('engaged', 0):,} owned or listened to, {sc.get('owned', 0):,} owned")
    if phase == "classifying":
        return f"{job}: updating artist classifiers…"
    if phase == "done":
        summ = ev.get("summary")
        if summ and "imported" in ev:
            got = ev.get("imported") or {}
            return (f"{job} done: {sum(got.values()):,} records through the gate from "
                    f"{summ.get('albums', 0):,} albums" if got else f"{job} done: nothing to merge")
        if summ:
            return (f"{job} done: {ev.get('name')} ({fmt_bytes(int(ev.get('size') or 0))}, "
                    f"{summ.get('albums', 0):,} albums, {summ.get('analysed_tracks', 0):,} analysed)")
        return f"{job} done: {ev.get('name')} ({fmt_bytes(int(ev.get('size') or 0))})"
    if phase == "cancelled":
        return f"{job} cancelled"
    if phase == "error":
        return f"{job} failed: {ev.get('message')}"
    return f"{job}: {phase}"


class CliRun:
    """One `python -m backup <args>` child: start, stream its JSON events to
    `on_event` (reader-thread context — marshal to Tk yourself), cancel.
    Terminal events are `done` / `plan`, `cancelled` and `error`; a child
    that dies without one gets an `error` synthesised from its exit code."""

    def __init__(self, service_manager, args: list, on_event: Callable[[dict], None],
                 *, password: Optional[str] = None, job: str = "Backup"):
        self._sm = service_manager
        self._args = list(args)
        self._password = password
        self._on_event = on_event
        self.job = job
        self._proc: Optional[subprocess.Popen] = None
        self._thread: Optional[threading.Thread] = None
        self.last_event: Optional[dict] = None
        self.result: Optional[dict] = None

    @classmethod
    def backup(cls, service_manager, password: str, on_event) -> "CliRun":
        return cls(service_manager, ["create", "--password-env", PASSWORD_ENV],
                   on_event, password=password, job="Backup")

    @classmethod
    def export(cls, service_manager, scope: str, artists: list, on_event) -> "CliRun":
        args = ["export"] + ([a for name in artists for a in ("--artist", name)]
                             if artists else ["--scope", scope])
        return cls(service_manager, args, on_event, job="Export")

    @classmethod
    def plan_export(cls, service_manager, on_event) -> "CliRun":
        return cls(service_manager, ["export", "--plan"], on_event, job="Export")

    @classmethod
    def plan_import(cls, service_manager, path, on_event) -> "CliRun":
        return cls(service_manager, ["import", str(path), "--dry-run"], on_event, job="Import")

    @classmethod
    def apply_import(cls, service_manager, path, on_event, *, existing_only: bool = False) -> "CliRun":
        args = ["import", str(path), "--yes"] + (["--existing-only"] if existing_only else [])
        return cls(service_manager, args, on_event, job="Import")

    @property
    def running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def start(self) -> None:
        env = self._sm.backend_env()
        if self._password is not None:
            env[PASSWORD_ENV] = self._password
            self._password = None
        cmd = ([self._sm._get_backend_python(), "-m", "backup"] + self._args
               + ["--progress-json", "--cancel-on-stdin"])
        # stderr rides the same pipe as the events: a second pipe nobody
        # drains until the first closes is a deadlock waiting for a chatty
        # child (Windows pipes hold 4 KB). Non-JSON lines are kept for the
        # error report and logged at debug.
        kwargs = {"cwd": str(self._sm._backend_dir), "env": env,
                  "stdin": subprocess.PIPE, "stdout": subprocess.PIPE,
                  "stderr": subprocess.STDOUT, "text": True, "encoding": "utf-8",
                  "errors": "replace", "bufsize": 1}
        if sys.platform == "win32":
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
        self._proc = subprocess.Popen(cmd, **kwargs)
        logger.info("%s started (PID %d): backup %s", self.job, self._proc.pid, self._args[0])
        self._thread = threading.Thread(target=self._pump, daemon=True, name="backup-cli")
        self._thread.start()

    def _emit(self, ev: dict) -> None:
        self.last_event = ev
        try:
            self._on_event(ev)
        except Exception as e:                      # a UI callback must not kill the reader
            logger.debug("backup event callback failed: %s", e)

    def _pump(self) -> None:
        proc = self._proc
        terminal = False
        noise: list = []
        for line in proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except ValueError:
                noise.append(line)
                del noise[:-20]
                logger.debug("%s: %s", self.job, line)
                continue
            if ev.get("phase") in ("done", "plan"):
                self.result = ev
            if ev.get("phase") in ("done", "plan", "cancelled", "error"):
                terminal = True
            self._emit(ev)
        rc = proc.wait()
        if rc and noise:
            logger.error("%s output before exit %d: %s", self.job, rc, " | ".join(noise[-5:]))
        if not terminal:
            self._emit({"phase": "error",
                        "message": f"{self.job.lower()} process exited with code {rc}"
                                   + (f": {noise[-1]}" if noise else "")})
        logger.info("%s finished (exit %d)", self.job, rc)

    def cancel(self) -> None:
        if not self.running:
            return
        try:
            self._proc.stdin.write("cancel\n")
            self._proc.stdin.flush()
        except (OSError, ValueError) as e:
            logger.debug("%s cancel write failed: %s", self.job, e)

    def wait(self, timeout: Optional[float] = None) -> None:
        if self._thread is not None:
            self._thread.join(timeout)
