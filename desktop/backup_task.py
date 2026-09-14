"""Create a backup from the launcher — by running the CLI, never beside it.

The launcher owns no backup code of its own: Settings › Maintenance ›
"Create backup…" runs the same `python -m backup create` a Docker node's
weekly task runs (backend/backup.py), on the backend interpreter with the
backend's own environment (service_manager.backend_env → backend.env: the
DSN, the identity dir, BACKUP_DIR=<data_dir>/backup, PG_BIN=pgsql/bin), and
reads its progress as JSON lines. The account password travels in the
child's environment (`--password-env`), never on a command line where a
process list would show it; cancel is a line on the child's stdin
(`--cancel-on-stdin`), and the end of that pipe — the launcher going away —
ends the job too, so a quit mid-backup leaves no half-written file.
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


def fmt_bytes(n: int) -> str:
    if n >= 1024 ** 3:
        return f"{n / 1024 ** 3:.1f} GB"
    return f"{n / 1024 ** 2:.0f} MB"


def describe_event(ev: dict) -> str:
    """One line of launcher UI per CLI event."""
    phase = ev.get("phase")
    if phase == "counting":
        return "Backup: taking a snapshot and counting rows…"
    if phase == "dumping":
        return f"Backup: {fmt_bytes(int(ev.get('bytes') or 0))} written…"
    if phase == "paused":
        return "Backup paused while music plays — resumes when playback stops"
    if phase == "identity":
        return "Backup: adding identity documents…"
    if phase == "done":
        return f"Backup done: {ev.get('name')} ({fmt_bytes(int(ev.get('size') or 0))})"
    if phase == "cancelled":
        return "Backup cancelled"
    if phase == "error":
        return f"Backup failed: {ev.get('message')}"
    return f"Backup: {phase}"


class BackupRun:
    """One `python -m backup create` child: start, stream its events to
    `on_event` (reader-thread context — marshal to Tk yourself), cancel.
    Terminal events are `done`, `cancelled` and `error`; a child that dies
    without one gets an `error` synthesised from its exit code."""

    def __init__(self, service_manager, password: str,
                 on_event: Callable[[dict], None]):
        self._sm = service_manager
        self._password = password
        self._on_event = on_event
        self._proc: Optional[subprocess.Popen] = None
        self._thread: Optional[threading.Thread] = None
        self.last_event: Optional[dict] = None
        self.result: Optional[dict] = None

    @property
    def running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def start(self) -> None:
        env = self._sm.backend_env()
        env[PASSWORD_ENV] = self._password
        self._password = ""
        cmd = [self._sm._get_backend_python(), "-m", "backup", "create",
               "--password-env", PASSWORD_ENV, "--progress-json", "--cancel-on-stdin"]
        kwargs = {"cwd": str(self._sm._backend_dir), "env": env,
                  "stdin": subprocess.PIPE, "stdout": subprocess.PIPE,
                  "stderr": subprocess.PIPE, "text": True, "encoding": "utf-8",
                  "errors": "replace", "bufsize": 1}
        if sys.platform == "win32":
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
        self._proc = subprocess.Popen(cmd, **kwargs)
        logger.info("backup started (PID %d)", self._proc.pid)
        self._thread = threading.Thread(target=self._pump, daemon=True, name="backup-run")
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
        for line in proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except ValueError:
                logger.debug("backup: %s", line)
                continue
            if ev.get("phase") == "done":
                self.result = ev
            if ev.get("phase") in ("done", "cancelled", "error"):
                terminal = True
            self._emit(ev)
        stderr = proc.stderr.read()
        rc = proc.wait()
        if stderr.strip():
            logger.log(logging.ERROR if rc else logging.DEBUG, "backup stderr: %s", stderr.strip()[-2000:])
        if not terminal:
            self._emit({"phase": "error",
                        "message": f"backup process exited with code {rc}"
                                   + (f": {stderr.strip().splitlines()[-1]}" if stderr.strip() else "")})
        logger.info("backup finished (exit %d)", rc)

    def cancel(self) -> None:
        if not self.running:
            return
        try:
            self._proc.stdin.write("cancel\n")
            self._proc.stdin.flush()
        except (OSError, ValueError) as e:
            logger.debug("backup cancel write failed: %s", e)

    def wait(self, timeout: Optional[float] = None) -> None:
        if self._thread is not None:
            self._thread.join(timeout)
