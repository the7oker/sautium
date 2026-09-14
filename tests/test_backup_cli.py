"""The backup CLI's contract with its callers (backend/backup.py,
desktop/backup_task.py, desktop/config_manager.load_env_file): the JSON
event lines the launcher reads, the terminal counter the weekly task's log
filter strips, the stdin cancel, the env-file bootstrap. The job itself runs
against the live database as `python -m backup selftest`."""

import io
import json
import sys
import threading

import pytest

from desktop import backup_task
from desktop.config_manager import load_env_file

REPO_BACKEND = None


@pytest.fixture(scope="module")
def backup_mod():
    """backend/backup.py imports `config` (backend flat import) at load."""
    from pathlib import Path
    backend = Path(__file__).resolve().parent.parent / "backend"
    if str(backend) not in sys.path:
        sys.path.insert(0, str(backend))
    return pytest.importorskip("backup")


def test_json_progress_lines_are_one_object_each(backup_mod, capsys):
    p = backup_mod.JsonProgress()
    p("counting")
    p("dumping", bytes=1024)
    p("paused", reason="playback")
    p.done({"path": backup_mod.Path("/x/a.sbk"), "size": 10, "sha256": "ab", "dump_size": 9})
    p.failed("boom")
    p.cancelled()
    lines = capsys.readouterr().out.strip().splitlines()
    events = [json.loads(line) for line in lines]
    assert events == [
        {"phase": "counting"},
        {"phase": "dumping", "bytes": 1024},
        {"phase": "paused", "reason": "playback"},
        {"phase": "done", "path": "/x/a.sbk", "name": "a.sbk", "size": 10, "sha256": "ab", "dump_size": 9},
        {"phase": "error", "message": "boom"},
        {"phase": "cancelled"},
    ]


def test_human_progress_keeps_the_words_the_weekly_filter_strips(backup_mod, capsys):
    p = backup_mod.HumanProgress()
    p("counting")
    p("dumping", bytes=5_000_000)
    p("identity")
    out = capsys.readouterr().out.replace("\r", "\n")
    kept = [ln.strip() for ln in out.splitlines() if ln.strip()]
    assert kept == ["counting…", "dumping… 5 MB", "identity…"]


def test_stdin_cancel_on_the_word_and_on_eof(backup_mod):
    token = backup_mod.CancelToken()
    t = backup_mod.watch_stdin_for_cancel(token, stream=io.StringIO("noise\nCancel\nmore\n"))
    t.join(2)
    assert token.event.is_set()
    token2 = backup_mod.CancelToken()
    t2 = backup_mod.watch_stdin_for_cancel(token2, stream=io.StringIO(""))
    t2.join(2)
    assert token2.event.is_set()                 # the parent went away


def test_cancel_token_aborts_a_blocked_hold(backup_mod):
    class Hold:
        cancelled = False
        def cancel(self): self.cancelled = True
    token = backup_mod.CancelToken()
    token._hold = Hold()
    token.request()
    assert token.event.is_set() and token._hold.cancelled


def test_load_env_file_skips_comments_and_blanks(tmp_path):
    env = tmp_path / "backend.env"
    env.write_text("# generated\n\nPOSTGRES_DB=sautium\nPG_BIN=C:\\x\\pgsql\\bin\n"
                   "EMPTY=\nBROKEN LINE\n", encoding="utf-8")
    assert load_env_file(env) == {"POSTGRES_DB": "sautium", "PG_BIN": "C:\\x\\pgsql\\bin", "EMPTY": ""}
    assert load_env_file(tmp_path / "missing.env") == {}


def test_describe_event_and_latest_backup(tmp_path):
    assert backup_task.describe_event({"phase": "dumping", "bytes": 2 * 1024 ** 3}).startswith("Backup: 2.0 GB")
    assert "paused" in backup_task.describe_event({"phase": "paused", "reason": "playback"})
    assert backup_task.describe_event({"phase": "done", "name": "a.sbk", "size": 5 * 1024 ** 2}) == "Backup done: a.sbk (5 MB)"
    assert backup_task.describe_event({"phase": "error", "message": "x"}) == "Backup failed: x"
    assert backup_task.latest_backup(tmp_path) is None
    from desktop import node_backup as nb
    kdf = nb.KdfParams(time_cost=1, memory_cost=8 * 1024, parallelism=1)
    for name in ("old.sbk", "new.sbk"):
        with open(tmp_path / name, "wb") as fp:
            w = nb.BackupWriter(fp, kek=nb.derive_kek("pw", "vale", kdf), username="vale",
                                pubkey="ab" * 32, kdf=kdf, chunk_size=64, created_at="2026-09-14T00:00:00Z")
            w.add_member(nb.MANIFEST_MEMBER, b"{}")
            w.finish()
    import os, time
    os.utime(tmp_path / "old.sbk", (time.time() - 100, time.time() - 100))
    last = backup_task.latest_backup(tmp_path)
    assert last["name"] == "new.sbk" and last["username"] == "vale" and last["created_at"] == "2026-09-14T00:00:00Z"
