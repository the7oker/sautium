""""Back up now" from the Web UI (backend/backup_job.py, routers/backup.py;
PI-NODE-2B §4.2): the making only.

- The account's password reaches the job in the child's environment alone,
  with the database's login the lifespan dropped from this process's; this
  process's own environment never holds it.
- One job at a time; its progress lines become the state; a cancel is the
  child's stdin closing; an error is its own sentence.
- The route takes the password through the credential box and the signed
  door (a wrong one refused before any job), an account with no password
  has nothing to key a backup on.
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
    monkeypatch.setattr(settings, "p2p_username", "")
    monkeypatch.setattr(settings, "p2p_password", "")
    monkeypatch.setattr(settings, "p2p_email", "")
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


def test_the_password_lives_in_the_jobs_environment_alone(job):
    assert backup_job.start("the account's") is True
    ended = _ended()
    assert ended["done"] == {"name": "sautium-backup-abc-2026-11-19.sbk", "size": 4000}
    assert ended["error"] is None and job.woken
    seen = json.loads(job.seen.read_text())
    assert seen["env"] == {"SAUTIUM_BACKUP_PASSWORD": "the account's", "POSTGRES_PASSWORD": "the database's",
                           "POSTGRES_USER": "musicai", "P2P_PASSWORD": None}
    assert "the account's" not in os.environ.values()
    assert "SAUTIUM_BACKUP_PASSWORD" not in os.environ and "POSTGRES_PASSWORD" not in os.environ
    # The command names the variable, never the password (the module's own,
    # read from its source: the fixture's stands in for it here)
    assert seen["argv"] == ["create"]
    source = (BACKEND / "backup_job.py").read_text(encoding="utf-8")
    assert '"--password-env", PASSWORD_ENV' in source and '"--cancel-on-stdin"' in source


def test_an_account_given_in_env_is_handed_to_the_job_too(job, monkeypatch):
    monkeypatch.setattr(settings, "p2p_username", "owner")
    monkeypatch.setattr(settings, "p2p_password", "the env account's")
    env = backup_job._child_env("typed")
    assert (env["P2P_USERNAME"], env["P2P_PASSWORD"], env["SAUTIUM_BACKUP_PASSWORD"]) == (
        "owner", "the env account's", "typed")
    assert "P2P_EMAIL" not in env


def test_one_job_at_a_time_and_a_cancel_is_the_pipes_end(job, monkeypatch):
    monkeypatch.setenv("STAND_IN_MODE", "wait")
    assert backup_job.start("pw") is True
    assert backup_job.start("pw") is False                       # a second tap: one is running
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
    assert backup_job.start("pw") is True                        # and the next one may
    backup_job.cancel()
    _ended()


def test_a_job_that_fails_says_its_own_sentence(job, monkeypatch):
    monkeypatch.setenv("STAND_IN_MODE", "error")
    backup_job.start("pw")
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
    backup_job.start("pw")
    with pytest.raises(RuntimeError, match="a backup is running"):
        backup_job.keep_newest()
    backup_job.cancel()
    _ended()
    assert backup_job.state()["count"] == 5


def test_the_route_asks_the_password_at_the_signed_door_before_any_job(job, monkeypatch):
    import asyncio

    import device_auth
    from fastapi import HTTPException
    from routers import backup as backup_router
    from routers.auth import StepUp
    started = []
    monkeypatch.setattr(backup_job, "start", lambda password: started.append(password) or True)

    async def refuse(password, pin):
        raise device_auth.WrongPassword()
    monkeypatch.setattr(device_auth, "step_up", refuse)

    def ask(**given):
        return asyncio.run(backup_router.post_backup(opened=(StepUp(**given), lambda body: body)))
    with pytest.raises(HTTPException) as refused:
        ask(password="a guess")
    assert refused.value.status_code == 403 and refused.value.headers["X-Sautium-Auth-Error"] == "step-up"
    assert started == []

    async def vouch(password, pin):
        return None
    monkeypatch.setattr(device_auth, "step_up", vouch)
    with pytest.raises(HTTPException) as refused:
        ask(pin="ABCD-EFGH")                                     # the host's code: no password to key on
    assert refused.value.status_code == 409 and "no password" in refused.value.detail and started == []
    assert ask(password="the right one")["job"] is not None and started == ["the right one"]
    monkeypatch.setattr(backup_job, "start", lambda password: False)
    with pytest.raises(HTTPException) as refused:
        ask(password="the right one")
    assert refused.value.status_code == 409 and refused.value.detail == "A backup is running"


def test_the_password_is_taken_only_through_the_box():
    from routers import backup as backup_router
    source = (BACKEND / "routers" / "backup.py").read_text(encoding="utf-8")
    assert "async def post_backup(opened=boxed(StepUp))" in source and "await step_up(req)" in source
    posts = [r for r in backup_router.router.routes if "POST" in r.methods]
    assert sorted(r.path for r in posts) == ["/api/backup", "/api/backup/cancel", "/api/backup/keep-newest"]


def test_no_route_serves_a_file_from_the_backup_folder():
    """The file holds the node's identity under the password's strength
    alone: it leaves by a disk or a share, never by HTTP. Held three ways:
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
