"""A backup made from the Web UI (PI-NODE-2B §4.2): the making only.

The file stays on the node — `data/backup`, its own bind — and leaves by a
disk the person plugs in or, later, a share; no route serves it. What runs is
the one implementation of "make a backup": `python -m backup create
--password-env … --progress-json --cancel-on-stdin` (backend/backup.py), a
child of this process, as the launcher runs it (desktop/backup_task.py).

The account's password reaches the child in ITS environment alone: this
process's own environment never holds it. The child also gets back what the
lifespan dropped from this process's environment (serving.hold_secrets) and a
backup needs — the database's login and, for an account given in .env, the
account itself — and nothing else of config.SECRET_ENV. The child makes
itself undumpable and pops the password as it starts (backup.main,
backup._password).

One job at a time. Its progress is process memory, said on the Library
stream; a cancel closes the child's stdin, which the child reads as the
parent's word (backup.watch_stdin_for_cancel) and removes its part file —
as it does when this process ends, the pipe's other end going with it.
"""

import json
import logging
import os
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any, Dict, Optional

from config import settings
from desktop import node_backup as nb

logger = logging.getLogger(__name__)

PASSWORD_ENV = "SAUTIUM_BACKUP_PASSWORD"
# How many of the newest backups "Keep the newest" leaves
KEEP_NEWEST = 3

_lock = threading.Lock()
_proc: Optional[subprocess.Popen] = None
_state: Dict[str, Any] = {"running": False, "phase": None, "bytes": None, "total": None,
                          "cancel_requested": False, "error": None, "done": None}


def _wake() -> None:
    from routers.settings import notify_library_subscribers
    notify_library_subscribers()


def _child_env(password: str) -> Dict[str, str]:
    """This process's environment, the backup's password, and what a backup
    needs of the credentials the lifespan dropped from it."""
    env = dict(os.environ)
    env[PASSWORD_ENV] = password
    env["POSTGRES_USER"] = settings.postgres_user
    env["POSTGRES_PASSWORD"] = settings.postgres_password
    # An account given in .env has no key on disk: the job derives it as
    # this process did
    for name, value in (("P2P_USERNAME", settings.p2p_username), ("P2P_PASSWORD", settings.p2p_password),
                        ("P2P_EMAIL", settings.p2p_email)):
        if value:
            env[name] = value
    return env


def _command() -> list:
    return [sys.executable, "-m", "backup", "create", "--password-env", PASSWORD_ENV,
            "--progress-json", "--cancel-on-stdin"]


def start(password: str) -> bool:
    """Start the job; False when one is running. The password was checked at
    the signed door by the caller; the job checks it again as its own first
    step, as every caller's does."""
    global _proc
    with _lock:
        if _state["running"]:
            return False
        proc = subprocess.Popen(
            _command(), cwd=str(Path(__file__).resolve().parent), env=_child_env(password),
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,      # stderr: the node's own log
            text=True, encoding="utf-8", errors="replace")
        _proc = proc
        _state.update(running=True, phase="starting", bytes=None, total=None,
                      cancel_requested=False, error=None, done=None)
    threading.Thread(target=_follow, args=(proc,), name="backup-job", daemon=True).start()
    logger.info("backup started from the Web UI")
    _wake()
    return True


def _follow(proc: subprocess.Popen) -> None:
    """The child's progress lines into the state, until it ends."""
    global _proc
    last: Dict[str, Any] = {}
    try:
        for line in proc.stdout:
            try:
                last = json.loads(line)
            except ValueError:
                continue
            if not isinstance(last, dict):
                continue
            phase = last.get("phase")
            if phase in ("done", "error", "cancelled"):
                continue                        # said once the process has ended, below
            with _lock:
                _state.update(phase=phase, bytes=last.get("bytes"), total=last.get("total"))
            _wake()
    finally:
        code = proc.wait()
        with _lock:
            _proc = None
            phase = last.get("phase") if isinstance(last, dict) else None
            if code == 0 and phase == "done":
                _state.update(done={"name": str(last.get("name", ""))[:200], "size": last.get("size")},
                              error=None)
                logger.info("backup written: %s", last.get("name"))
            elif phase == "cancelled" or _state["cancel_requested"]:
                _state.update(error=None)
                logger.info("backup cancelled")
            else:
                message = str(last.get("message") or "the backup did not finish")[:500] \
                    if phase == "error" else "the backup did not finish"
                _state.update(error=message)
                logger.warning("backup failed: %s", message)
            _state.update(running=False, phase=None, bytes=None, total=None)
        _wake()


def cancel() -> bool:
    """Ask the running job to stop: its stdin closed is the parent's word."""
    with _lock:
        if not _state["running"] or _proc is None:
            return False
        _state["cancel_requested"] = True
        try:
            _proc.stdin.close()
        except OSError:
            pass
    _wake()
    return True


def _files() -> list:
    """The backups in the node's folder, newest last: real files of the
    backup's own suffix, never a link."""
    folder = Path(settings.backup_dir)
    found = []
    try:
        with os.scandir(folder) as it:
            for entry in it:
                if entry.name.endswith(nb.FILE_SUFFIX) and entry.is_file(follow_symlinks=False):
                    st = entry.stat(follow_symlinks=False)
                    found.append((st.st_mtime, entry.name, st.st_size))
    except OSError:
        return []
    return sorted(found)


def state() -> Dict[str, Any]:
    """What the Backup row and its sheet read: the job, and the folder's
    files — how many, how much, the newest."""
    files = _files()
    from datetime import datetime, timezone
    newest = files[-1] if files else None
    with _lock:
        job = dict(_state)
    return {
        "job": job,
        "count": len(files),
        "bytes": sum(size for _, _, size in files),
        "newest": ({"name": newest[1], "size": newest[2],
                    "at": datetime.fromtimestamp(newest[0], timezone.utc).isoformat()}
                   if newest else None),
        # Where the files are, as the node's own path: said to the person,
        # who copies them off the machine
        "folder": settings.backup_dir,
        "keep_newest": KEEP_NEWEST,
    }


def keep_newest() -> Dict[str, Any]:
    """The owner's "Keep the newest N": every older backup removed, by name
    within the folder alone. Refused while a job writes there."""
    with _lock:
        if _state["running"]:
            raise RuntimeError("a backup is running")
    folder = Path(settings.backup_dir)
    removed, freed = 0, 0
    for _, name, size in _files()[:-KEEP_NEWEST]:
        try:
            os.unlink(folder / name)
        except OSError as e:
            logger.warning("old backup %s not removed: %s", name, e)
            continue
        removed += 1
        freed += size
    logger.info("old backups removed: %d (%d bytes)", removed, freed)
    _wake()
    return {"removed": removed, "freed_bytes": freed}
