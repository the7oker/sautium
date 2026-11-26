""""Back up now" from the Web UI (backend/backup_job.py, routers/backup.py;
PI-NODE-2B §4.2): the making only.

- Nothing is typed (SEC-S5: the file is keyed on the node's own seed): the
  job gets no password, by pipe, environment or argument, and the database's
  login the lifespan dropped from this process's environment.
- One job at a time; its progress lines become the state; a cancel is the
  child's stdin closing; an error is its own sentence.
- The route asks no password and no step-up (§14 Q9): it starts the job,
  single-flight.
- "Keep the newest" removes exactly the older files of the folder, real
  backup files alone.
- No route of the app serves a file from the backup folder.
The job here is a stand-in script speaking the CLI's lines; the CLI itself
is test_node_backup's and the launcher's.
"""

import ast
import json
import os
import sys
import threading
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
BACKEND = REPO / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

pytest.importorskip("psycopg2")
backup_job = pytest.importorskip("backup_job")
from config import settings  # noqa: E402

STAND_IN = r"""
import json, os, sys
seen = {k: os.environ.get(k) for k in ("SAUTIUM_BACKUP_PASSWORD", "POSTGRES_PASSWORD", "POSTGRES_USER", "P2P_PASSWORD")}
open(os.environ["STAND_IN_OUT"], "w").write(json.dumps({"env": seen, "argv": sys.argv[1:]}))
def say(**line):
    sys.stdout.write(json.dumps(line) + "\n"); sys.stdout.flush()
say(phase="counting")
say(phase="dumping", bytes=1000, total=4000)
mode = os.environ["STAND_IN_MODE"]
if mode == "done":
    say(phase="done", name="sautium-backup-abc-2026-11-19.sbk", size=4000, sha256="0" * 64)
    sys.exit(0)
if mode == "error":
    say(phase="error", message="not enough space in /app/data/backup: 1.0 GB free, 3.0 GB needed")
    sys.exit(1)
for line in sys.stdin:                      # a cancel is this pipe's end
    pass
say(phase="cancelled")
sys.exit(1)
"""


@pytest.fixture
def job(tmp_path, monkeypatch):
    script = tmp_path / "stand_in.py"
    script.write_text(STAND_IN)
    out = tmp_path / "seen.json"
    folder = tmp_path / "backup"
    folder.mkdir()
    monkeypatch.setattr(settings, "backup_dir", str(folder))
    monkeypatch.setattr(settings, "postgres_user", "musicai")
    monkeypatch.setattr(settings, "postgres_password", "the database's")
    monkeypatch.setattr(backup_job, "_command", lambda: [sys.executable, str(script), "create"])
    monkeypatch.setattr(backup_job, "_wake", lambda: woken.append(1))
    monkeypatch.setattr(backup_job, "_proc", None)
    monkeypatch.setattr(backup_job, "_state", {"running": False, "phase": None, "bytes": None,
                                               "total": None, "cancel_requested": False,
                                               "error": None, "done": None})
    monkeypatch.setenv("STAND_IN_OUT", str(out))
    monkeypatch.setenv("STAND_IN_MODE", "done")
    monkeypatch.delenv("POSTGRES_PASSWORD", raising=False)       # as after serving.hold_secrets
    monkeypatch.delenv("SAUTIUM_BACKUP_PASSWORD", raising=False)
    woken: list = []
    return type("Job", (), {"folder": folder, "seen": out, "woken": woken})


def _ended(seconds: float = 20) -> dict:
    deadline = time.monotonic() + seconds
    while backup_job.state()["job"]["running"]:
        assert time.monotonic() < deadline, backup_job.state()
        time.sleep(0.02)
    return backup_job.state()["job"]


def test_the_job_gets_no_password_and_the_databases_login(job):
    assert backup_job.start() is True
    ended = _ended()
    assert ended["done"] == {"name": "sautium-backup-abc-2026-11-19.sbk", "size": 4000}
    assert ended["error"] is None and job.woken
    seen = json.loads(job.seen.read_text())
    assert seen["env"] == {"SAUTIUM_BACKUP_PASSWORD": None, "POSTGRES_PASSWORD": "the database's",
                           "POSTGRES_USER": "musicai", "P2P_PASSWORD": None}
    assert "POSTGRES_PASSWORD" not in os.environ
    assert seen["argv"] == ["create"]
    source = (BACKEND / "backup_job.py").read_text(encoding="utf-8")
    assert '"create", "--progress-json", "--cancel-on-stdin"' in source
    assert "--password" not in source and "stdin.write" not in source
    assert "p2p_password" not in source and "P2P_" not in source


def test_one_job_at_a_time_and_a_cancel_is_the_pipes_end(job, monkeypatch):
    monkeypatch.setenv("STAND_IN_MODE", "wait")
    assert backup_job.start() is True
    assert backup_job.start() is False                       # a second tap: one is running
    deadline = time.monotonic() + 20
    while backup_job.state()["job"]["phase"] != "dumping":
        assert time.monotonic() < deadline
        time.sleep(0.02)
    running = backup_job.state()["job"]
    assert (running["bytes"], running["total"], running["running"]) == (1000, 4000, True)
    assert backup_job.cancel() is True
    ended = _ended()
    assert (ended["cancel_requested"], ended["error"], ended["done"]) == (True, None, None)
    assert backup_job.cancel() is False                          # nothing runs
    assert backup_job.start() is True                        # and the next one may
    backup_job.cancel()
    _ended()


def test_a_job_that_fails_says_its_own_sentence(job, monkeypatch):
    monkeypatch.setenv("STAND_IN_MODE", "error")
    backup_job.start()
    ended = _ended()
    assert ended["error"].startswith("not enough space in /app/data/backup") and ended["done"] is None


def _backups(folder: Path, n: int) -> list:
    names = []
    for i in range(n):
        path = folder / f"sautium-backup-abc-2026-11-{10 + i}.sbk"
        path.write_bytes(b"x" * (100 + i))
        os.utime(path, (1_700_000_000 + i * 86400,) * 2)
        names.append(path.name)
    return names


def test_keep_the_newest_removes_exactly_the_older_backups(job, tmp_path):
    names = _backups(job.folder, 5)
    (job.folder / "notes.txt").write_text("the person's")
    (job.folder / "x.sbk.part").write_bytes(b"a job's")
    outside = tmp_path / "elsewhere.sbk"
    outside.write_bytes(b"not here")
    (job.folder / "linked.sbk").symlink_to(outside)
    state = backup_job.state()
    assert (state["count"], state["bytes"], state["newest"]["name"]) == (5, 510, names[-1])
    assert backup_job.keep_newest() == {"removed": 2, "freed_bytes": 201}
    left = sorted(p.name for p in job.folder.iterdir())
    assert left == sorted(names[2:] + ["linked.sbk", "notes.txt", "x.sbk.part"])
    assert outside.read_bytes() == b"not here"
    assert backup_job.keep_newest() == {"removed": 0, "freed_bytes": 0}
    assert backup_job.state()["count"] == 3


def test_nothing_is_removed_while_a_job_writes(job, monkeypatch):
    _backups(job.folder, 5)
    monkeypatch.setenv("STAND_IN_MODE", "wait")
    backup_job.start()
    with pytest.raises(RuntimeError, match="a backup is running"):
        backup_job.keep_newest()
    backup_job.cancel()
    _ended()
    assert backup_job.state()["count"] == 5


def test_the_route_starts_the_job_with_no_password_and_no_step_up(job, monkeypatch):
    from fastapi import HTTPException
    from routers import backup as backup_router
    started = []
    monkeypatch.setattr(backup_job, "start", lambda: started.append(1) or True)
    assert backup_router.post_backup()["job"] is not None and started == [1]
    monkeypatch.setattr(backup_job, "start", lambda: False)
    with pytest.raises(HTTPException) as refused:
        backup_router.post_backup()
    assert refused.value.status_code == 409 and refused.value.detail == "A backup is running"
    source = (BACKEND / "routers" / "backup.py").read_text(encoding="utf-8")
    assert "step_up" not in source and "boxed" not in source and "password" not in source
    posts = [r for r in backup_router.router.routes if "POST" in r.methods]
    assert sorted(r.path for r in posts) == ["/api/backup", "/api/backup/cancel", "/api/backup/keep-newest"]


def test_the_page_asks_no_password_to_back_up():
    shell = (BACKEND / "static" / "app-shell.js").read_text(encoding="utf-8")
    sheet = shell[shell.index("function openBackupSheet()"):]
    sheet = sheet[:sheet.index("\n  }\n")]
    assert "fetch('/api/backup', { method: 'POST' })" in sheet
    assert "askStepUp" not in sheet and "password" not in sheet.lower()


def test_no_route_serves_a_file_from_the_backup_folder():
    """The file holds the node's whole database: it leaves by a disk or a
    share, never by HTTP. Held three ways:
    the folder is named by the backup's own modules alone; neither of them
    nor the routes builds a file or stream response; and the app's static
    mount is its own folder."""
    names = {}
    for path in list(BACKEND.glob("*.py")) + list((BACKEND / "routers").glob("*.py")):
        if path.name.startswith("test_"):
            continue
        text = path.read_text(encoding="utf-8")
        if "backup_dir" in text:
            names[path.relative_to(BACKEND).as_posix()] = text
    assert sorted(names) == ["backup.py", "backup_job.py", "config.py"]
    for name in ("backup_job.py", "routers/backup.py"):
        tree = ast.parse((BACKEND / name).read_text(encoding="utf-8"))
        used = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)} | {
            n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)} | {
            alias.name for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) for alias in n.names}
        assert not used & {"FileResponse", "StreamingResponse", "Response", "StaticFiles", "read_bytes"}, name
    from routers import backup as backup_router
    assert sorted((r.path, sorted(r.methods)) for r in backup_router.router.routes) == [
        ("/api/backup", ["GET"]), ("/api/backup", ["POST"]),
        ("/api/backup/cancel", ["POST"]), ("/api/backup/keep-newest", ["POST"])]
    main_source = (BACKEND / "main.py").read_text(encoding="utf-8")
    assert main_source.count("StaticFiles(") == 1 and 'StaticFiles(directory="static")' in main_source
