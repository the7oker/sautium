"""The backup CLI's contract with its callers (backend/backup.py,
desktop/backup_task.py, desktop/config_manager.load_env_file): the JSON
event lines the launcher reads, the terminal counter the weekly task's log
filter strips, the stdin cancel, the env-file bootstrap. The job itself runs
against the live database as `python -m backup selftest`."""

import io
import json
import sys
import threading
from pathlib import Path

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
    assert backup_task.describe_event({"phase": "plan", "scopes": {"analysed": 5000, "engaged": 4237, "owned": 3757}},
                                      "Export") == ("Export: 5,000 albums with analysis here, 4,237 owned or "
                                                    "listened to, 3,757 owned")
    assert backup_task.latest_backup(tmp_path) is None
    from desktop import node_backup as nb
    for name in ("old.sbk", "new.sbk"):
        with open(tmp_path / name, "wb") as fp:
            w = nb.BackupWriter(fp, seed=bytes(32), username="vale", chunk_size=64,
                                created_at="2026-09-14T00:00:00Z")
            w.add_member(nb.MANIFEST_MEMBER, b"{}")
            w.finish()
    import os, time
    os.utime(tmp_path / "old.sbk", (time.time() - 100, time.time() - 100))
    last = backup_task.latest_backup(tmp_path)
    assert last["name"] == "new.sbk" and last["username"] == "vale" and last["created_at"] == "2026-09-14T00:00:00Z"


def test_head_commit_reads_git_files_without_spawning(tmp_path):
    from desktop import updater
    sha = "0123456789abcdef0123456789abcdef01234567"
    git = tmp_path / ".git"
    (git / "refs" / "heads").mkdir(parents=True)
    (git / "HEAD").write_text("ref: refs/heads/main\n")
    assert updater.head_commit(tmp_path) is None            # ref not written yet
    (git / "refs" / "heads" / "main").write_text(sha + "\n")
    assert updater.head_commit(tmp_path) == sha[:12]
    (git / "refs" / "heads" / "main").unlink()
    (git / "packed-refs").write_text("# pack-refs with: peeled fully-peeled sorted \n"
                                     f"{sha} refs/heads/main\n^deadbeef\n")
    assert updater.head_commit(tmp_path) == sha[:12]
    (git / "HEAD").write_text(sha.replace("0", "f") + "\n")   # detached
    assert updater.head_commit(tmp_path) == sha.replace("0", "f")[:12]
    (git / "HEAD").write_text("garbage")
    assert updater.head_commit(tmp_path) is None
    assert updater.head_commit(tmp_path / "nowhere") is None
    # a linked worktree: .git is a file pointing at the real gitdir
    wt = tmp_path / "wt"; wt.mkdir()
    (wt / ".git").write_text(f"gitdir: {git}\n")
    (git / "HEAD").write_text("ref: refs/heads/main\n")
    assert updater.head_commit(wt) == sha[:12]


def test_the_cli_takes_no_password(backup_mod):
    """The file is keyed on the node's own seed (SEC-S5): no subcommand
    reads a password, from a variable, a pipe or a prompt."""
    for argv in (["create", "--password-env", "P2P_PASSWORD"], ["create", "--password-stdin"],
                 ["inspect", "x.sbk", "--no-password"], ["restore", "x.sbk", "--identity"],
                 ["merge", "x.sbk", "--password-env", "P2P_PASSWORD"]):
        with pytest.raises(SystemExit) as refused:
            backup_mod.parser().parse_args(argv)
        assert refused.value.code == 2, argv                     # argparse: no such option
    assert backup_mod.parser().parse_args(["inspect", "x.sbk", "--manifest"]).manifest
    source = Path(backup_mod.__file__).read_text(encoding="utf-8")
    assert "getpass" not in source and "verify_password" not in source
    assert "--password" not in Path(backup_task.__file__).read_text(encoding="utf-8")


@pytest.mark.skipif(sys.platform != "linux", reason="prctl is Linux's")
def test_the_backup_cli_holds_no_secret_where_its_user_reads_it(backup_mod):
    """It holds the node's seed: it is undumpable, and drops the node's
    credentials from its environment before pg_dump or pg_restore start."""
    import ctypes
    import os
    r, w = os.pipe()
    pid = os.fork()
    if pid == 0:
        os.close(r)
        os.environ["POSTGRES_PASSWORD"] = "a made-up database password"
        try:
            backup_mod.main(["--help"])
        except SystemExit:
            pass
        left = "POSTGRES_PASSWORD" in os.environ
        os.write(w, f"{ctypes.CDLL(None).prctl(3, 0, 0, 0, 0)} {left}".encode())
        os._exit(0)
    os.close(w)
    out = os.read(r, 32).decode()
    os.waitpid(pid, 0)
    assert out == "0 False"


class _Hold:
    def __init__(self, _dsn):
        pass

    def sole_maker(self):
        pass

    def wait(self, on_pause=None):
        pass

    def close(self):
        pass


def _key():
    from desktop.node_identity import key_from_seed
    return key_from_seed(bytes(range(32)))


def test_create_keys_the_backup_on_the_nodes_own_seed_an_anonymous_ones_too(backup_mod, tmp_path,
                                                                              monkeypatch):
    import p2p_identity
    from desktop import node_identity as ni
    ni.write_account(tmp_path, ni.new_account("anon-1a2b", "", source="form", anonymous=True))
    key = _key()
    monkeypatch.setattr(p2p_identity, "load_signing_key", lambda _settings: key)
    monkeypatch.setattr(backup_mod, "node", lambda: {"username": "anon-1a2b",
                                                     "pubkey": ni.public_hex(key)})
    monkeypatch.setattr(backup_mod, "identity_dir", lambda: tmp_path)
    monkeypatch.setattr(backup_mod, "PlaybackHold", _Hold)
    monkeypatch.setattr(backup_mod, "pg_target", lambda: None)
    monkeypatch.setattr(backup_mod, "build_identity", lambda: {"commit": "c" * 40, "build": "test"})
    made = []
    monkeypatch.setattr(backup_mod.nb, "create_backup", lambda out, **kw: made.append(kw) or {})
    backup_mod.create(out_dir=tmp_path / "out")
    assert made[0]["seed"] == bytes(range(32)) and made[0]["username"] == "anon-1a2b"


def test_a_node_with_no_key_yet_makes_no_backup_and_says_why(backup_mod, tmp_path, monkeypatch):
    import p2p_identity
    from desktop import node_identity as ni
    monkeypatch.setattr(p2p_identity, "load_signing_key", lambda _settings: None)
    monkeypatch.setattr(backup_mod, "identity_dir", lambda: tmp_path)
    monkeypatch.setattr(backup_mod, "PlaybackHold", _Hold)
    with pytest.raises(backup_mod.nb.Refused, match="no identity yet"):
        backup_mod.create(out_dir=tmp_path / "out")
    ni.write_account(tmp_path, ni.new_account("owner", "a made-up passphrase amber", source="form"))
    with pytest.raises(backup_mod.nb.Refused,
                       match="key is still being made — back up once it is ready"):
        backup_mod.create(out_dir=tmp_path / "out")
    assert not (tmp_path / "out").exists()


def test_one_backup_of_a_node_is_made_at_a_time_whoever_makes_it(backup_mod, scratch_dsn, tmp_path, monkeypatch):
    """The Web UI's job, the weekly task and a hand-run CLI are separate
    processes: the second is refused while the first holds the making's lock,
    and the first's part file — which a starting maker clears as a dead job's
    — is left alone."""
    import p2p_identity
    key = _key()
    monkeypatch.setattr(p2p_identity, "load_signing_key", lambda _settings: key)
    monkeypatch.setattr(backup_mod, "node", lambda: {"username": "owner", "pubkey": "ab" * 32})
    monkeypatch.setattr(type(backup_mod.settings), "database_url", property(lambda self: scratch_dsn))
    monkeypatch.setattr(backup_mod, "build_identity", lambda: {"commit": "c" * 40, "build": "test"})
    monkeypatch.setattr(backup_mod, "pg_target", lambda: None)
    monkeypatch.setattr(backup_mod, "identity_dir", lambda: tmp_path)
    made = []
    monkeypatch.setattr(backup_mod.nb, "create_backup", lambda out, **kw: made.append(out) or {"name": "x.sbk"})
    part = tmp_path / ("running" + backup_mod.nb.PART_SUFFIX)
    part.write_bytes(b"the first maker's")

    first = backup_mod.PlaybackHold(scratch_dsn)
    first.sole_maker()
    try:
        with pytest.raises(backup_mod.nb.Refused, match="another backup of this node is being made"):
            backup_mod.create(out_dir=tmp_path)
        assert part.read_bytes() == b"the first maker's" and made == []
    finally:
        first.close()
    # The first ended (its session closed, however it ended): the next is made, a dead job's part cleared
    assert backup_mod.create(out_dir=tmp_path) == {"name": "x.sbk"}
    assert made == [tmp_path] and not part.exists()
    # … and its own lock went with its session
    again = backup_mod.PlaybackHold(scratch_dsn)
    again.sole_maker()
    again.close()
