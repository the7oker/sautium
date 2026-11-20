"""The Docker node runs as its own user (backend/node_user.py): the
container starts as root only to hand the node's folders to that user, then
drops root before anything else runs. An agent CLI never runs as root
(claude_code_runner.spawn_kwargs), and a process that starts one has dropped
the node's credentials from its environment first (serving.hold_secrets).
The drops are real ones, so those tests run where the suite runs as root, in
the backend's image."""

import ctypes
import os
import pwd
import subprocess
import sys
import types
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
if str(REPO / "backend") not in sys.path:
    sys.path.insert(0, str(REPO / "backend"))

import claude_code_runner  # noqa: E402

as_root_with_the_node_user = pytest.mark.skipif(
    sys.platform != "linux" or os.geteuid() != 0 or not any(
        p.pw_name == "agent" for p in pwd.getpwall()),
    reason="needs root and the image's node user")

SECRETS = {"POSTGRES_PASSWORD": "pg", "POSTGRES_USER": "u", "P2P_PASSWORD": "p2p",
           "P2P_USERNAME": "me", "P2P_EMAIL": "me@example.org", "LASTFM_SESSION_KEY": "s",
           "LASTFM_API_SECRET": "x", "GENIUS_ACCESS_TOKEN": "g", "OPENAI_COMPAT_API_KEY": "o",
           "ANTHROPIC_API_KEY": "a", "OPENAI_API_KEY": "k", "CODEX_API_KEY": "c",
           "postgres_password": "pg"}


def test_no_child_of_a_process_that_holds_the_secrets_inherits_one():
    import json
    from serving import hold_secrets

    def check():
        os.environ.update(SECRETS, CODEX_HOME="/somewhere", LANG="C.UTF-8")
        hold_secrets()
        child = subprocess.run([sys.executable, "-c", "import json, os; print(json.dumps(dict(os.environ)))"],
                               capture_output=True, text=True, check=True)
        return child.stdout

    env = json.loads(_in_a_child(check))
    assert set(env) & set(SECRETS) == set()
    assert {"PATH", "LANG", "CODEX_HOME"} <= set(env)


def test_every_credential_the_settings_read_is_held():
    import re
    from config import SECRET_ENV, Settings
    credentials = {name.upper() for name in Settings.model_fields
                   if re.search(r"password|secret|token|key$|_user$|username|email", name)}
    assert {"P2P_PASSWORD", "CODEX_API_KEY", "LASTFM_SESSION_KEY"} <= credentials <= SECRET_ENV


def test_the_processes_that_start_agents_hold_the_secrets_before_anything_else():
    import ast

    def first_call(body) -> str:
        for node in body:
            for n in ast.walk(node):
                if isinstance(n, ast.Call):
                    return ast.unparse(n.func)
        return ""

    main = ast.parse((REPO / "backend" / "main.py").read_text(encoding="utf-8"))
    lifespan = next(n for n in ast.walk(main) if isinstance(n, ast.AsyncFunctionDef)
                    and n.name == "lifespan")
    assert first_call(lifespan.body) == "hold_secrets"
    worker = ast.parse((REPO / "backend" / "gear_research_worker.py").read_text(encoding="utf-8"))
    cli = next(n for n in worker.body if isinstance(n, ast.If) and "__main__" in ast.unparse(n.test))
    assert first_call(cli.body) == "hold_secrets"


def test_reload_is_a_development_opt_in():
    env = {k: v for k, v in os.environ.items() if k != "UVICORN_RELOAD"}
    out = subprocess.run([sys.executable, "-c", "import entrypoint; print(entrypoint.RELOAD)"],
                         cwd=REPO / "backend", env=env, capture_output=True, text=True, check=True)
    assert out.stdout.strip() == "False"


def test_a_root_backend_starts_an_agent_as_the_node_user_with_no_other_group(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(claude_code_runner.os, "geteuid", lambda: 0)
    monkeypatch.setattr(claude_code_runner.pwd, "getpwnam", lambda name: types.SimpleNamespace(
        pw_name=name, pw_uid=1000, pw_gid=1000, pw_dir="/home/" + name))
    kwargs = claude_code_runner.spawn_kwargs({})
    assert (kwargs["user"], kwargs["group"], kwargs["extra_groups"]) == (1000, 1000, [])
    assert kwargs["env"]["HOME"] == "/home/agent"
    assert "preexec_fn" not in kwargs


def test_a_backend_that_is_the_node_user_starts_its_agents_as_itself(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(claude_code_runner.os, "geteuid", lambda: 1000)
    kwargs = claude_code_runner.spawn_kwargs({"HOME": "/home/agent"})
    assert not {"user", "group", "extra_groups", "preexec_fn"} & set(kwargs)


@as_root_with_the_node_user
def test_a_real_agent_child_of_a_root_backend_is_the_node_user_alone():
    agent = pwd.getpwnam("agent")
    kwargs = claude_code_runner.spawn_kwargs(dict(os.environ))
    out = subprocess.run(["sh", "-c", "id -u; id -G"], capture_output=True, text=True,
                         check=True, **kwargs).stdout.split("\n")
    assert out[0] == str(agent.pw_uid) and out[1] == str(agent.pw_gid)


def test_the_codex_runner_starts_its_children_the_same_way(monkeypatch):
    import codex_runner
    monkeypatch.setattr(codex_runner, "_codex_workdir", lambda: Path("/tmp"))
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(claude_code_runner.os, "geteuid", lambda: 0)
    monkeypatch.setattr(claude_code_runner.pwd, "getpwnam", lambda name: types.SimpleNamespace(
        pw_name=name, pw_uid=1000, pw_gid=1000, pw_dir="/home/" + name))
    kwargs = codex_runner._spawn_kwargs({"PATH": "/usr/bin"})
    assert (kwargs["user"], kwargs["cwd"], kwargs["stdin"]) == (1000, "/tmp", subprocess.DEVNULL)


def test_the_openai_providers_key_never_signs_codex_in(monkeypatch):
    """The Web UI saves the OpenAI provider's key into openai_api_key; Codex
    mints its sign-in only from what the environment gave the node."""
    from config import Settings
    monkeypatch.delenv("CODEX_API_KEY", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "key-of-the-environment")
    built = Settings(_env_file=None)
    built.openai_api_key = "key-the-web-ui-saved"           # routers/settings.py
    assert built.codex_mint_key == "key-of-the-environment"
    monkeypatch.delenv("OPENAI_API_KEY")
    built = Settings(_env_file=None)
    built.openai_api_key = "key-the-web-ui-saved"
    assert built.codex_mint_key is None


def test_a_key_the_web_ui_saved_never_runs_a_codex_sign_in(tmp_path, monkeypatch):
    import codex_cli
    import codex_runner
    from config import settings
    monkeypatch.setattr(settings, "_env_openai_api_key", None)
    monkeypatch.setattr(settings, "codex_api_key", None)
    monkeypatch.setattr(settings, "openai_api_key", "key-the-web-ui-saved")
    monkeypatch.setattr(codex_cli, "codex_home", lambda: tmp_path / "codex-home")
    monkeypatch.setattr(codex_runner, "_codex_home", lambda: tmp_path / "codex-home")
    monkeypatch.setattr(codex_cli, "get_codex_executable", lambda: None)
    fake = tmp_path / "codex"
    fake.write_text(f"#!/bin/sh\ntouch {tmp_path}/ran\n")
    fake.chmod(0o755)
    assert codex_cli.codex_auth_method() is None
    assert codex_runner._ensure_auth(str(fake)) == codex_runner.CODEX_LOGIN_MSG
    assert not (tmp_path / "ran").exists()


def test_codex_signs_in_with_the_settings_key_on_its_stdin_alone(tmp_path, monkeypatch):
    """The key reaches `codex login --with-api-key` on stdin, from the
    settings: the environment of a process that starts agents holds none."""
    import codex_cli
    import codex_runner
    from config import settings
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("CODEX_API_KEY", raising=False)
    monkeypatch.setattr(settings, "_env_openai_api_key", None)
    monkeypatch.setattr(settings, "codex_api_key", "key-from-the-settings")
    monkeypatch.setattr(codex_cli, "codex_home", lambda: tmp_path / "codex-home")
    monkeypatch.setattr(codex_runner, "_codex_home", lambda: tmp_path / "codex-home")
    monkeypatch.setattr(codex_runner, "_codex_workdir", lambda: tmp_path)
    monkeypatch.setattr(claude_code_runner.os, "geteuid", lambda: 1000)
    fake = tmp_path / "codex"
    fake.write_text(f"#!/bin/sh\ncat > {tmp_path}/stdin\nenv > {tmp_path}/env\n")
    fake.chmod(0o755)
    assert codex_cli.codex_auth_method() == "env_key"
    assert codex_runner._ensure_auth(str(fake)) is None
    assert (tmp_path / "stdin").read_text() == "key-from-the-settings"
    assert "key-from-the-settings" not in (tmp_path / "env").read_text()


# -- the image's entrypoint (backend/node_user.py) ----------------------------------

@pytest.fixture
def node_user(tmp_path, monkeypatch):
    """The image's entrypoint, its folders the test's own: run for real, as
    root, it hands them over and drops root, so a child process runs it."""
    import node_user
    monkeypatch.setattr(node_user, "NODE_TREES", (str(tmp_path / "data"),))
    monkeypatch.setattr(node_user, "NODE_FOLDERS", ())
    monkeypatch.setattr(node_user, "HOST_OWNED", str(tmp_path / "data"))   # root's: the uid stays
    (tmp_path / "data").mkdir()
    return node_user


@pytest.fixture
def accounts(tmp_path, monkeypatch, node_user):
    """Copies of the image's passwd and group for the entrypoint to renumber,
    and a home of the node user's for it to re-own: the real ones stay."""
    import shutil
    for name in ("passwd", "group"):
        shutil.copy(f"/etc/{name}", tmp_path / name)
    monkeypatch.setattr(node_user, "PASSWD", str(tmp_path / "passwd"))
    monkeypatch.setattr(node_user, "GROUP", str(tmp_path / "group"))
    agent = pwd.getpwnam("agent")
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    (home / ".claude" / "settings.json").write_text("{}")
    (home / ".cache").mkdir()                                # a bind mount, not the image's
    for path in (home, home / ".claude", home / ".claude" / "settings.json", home / ".cache"):
        os.chown(path, agent.pw_uid, agent.pw_gid)
    monkeypatch.setattr(node_user, "_mount_points", lambda: {str(home / ".cache")})
    return types.SimpleNamespace(passwd=tmp_path / "passwd", group=tmp_path / "group", home=home,
                                 pw=pwd.struct_passwd((*agent[:5], str(home), agent.pw_shell)))


def _entry(path, name):
    return next(line.split(":") for line in path.read_text().splitlines()
                if line.startswith(name + ":"))


@as_root_with_the_node_user
def test_the_node_user_takes_the_uid_of_the_host_user_its_data_is_for(tmp_path, node_user,
                                                                      accounts, capfd):
    """A native Linux host keeps its uids across a bind: the node's user
    becomes the owner of the checkout's backend/data, and the image's own
    files in its home follow; a mount there is no file of the image's."""
    agent = pwd.getpwnam("agent")
    data = tmp_path / "data"
    os.chown(data, 4242, 4243)
    pw = node_user.take_host_owner(accounts.pw, str(data), str(accounts.passwd), str(accounts.group))
    assert (pw.pw_name, pw.pw_uid, pw.pw_gid, pw.pw_dir) == ("agent", 4242, 4243, str(accounts.home))
    assert _entry(accounts.passwd, "agent")[2:4] == ["4242", "4243"]
    assert _entry(accounts.group, "agent")[2] == "4243"
    home = accounts.home
    assert [p.stat().st_uid for p in (home, home / ".claude", home / ".claude" / "settings.json",
                                      home / ".cache")] == [4242, 4242, 4242, agent.pw_uid]
    assert "takes uid 4242, gid 4243" in capfd.readouterr().err
    written = accounts.passwd.read_bytes(), accounts.group.read_bytes()
    assert node_user.take_host_owner(pw, str(data), str(accounts.passwd), str(accounts.group)) is pw
    assert (accounts.passwd.read_bytes(), accounts.group.read_bytes()) == written   # the next start


@as_root_with_the_node_user
def test_a_folder_root_owns_or_another_image_user_holds_leaves_the_uid(tmp_path, node_user,
                                                                      accounts, capfd):
    data = tmp_path / "data"
    before = accounts.passwd.read_bytes(), accounts.group.read_bytes()
    assert node_user.take_host_owner(accounts.pw, str(data), str(accounts.passwd),
                                     str(accounts.group)) is accounts.pw        # Docker made it
    nobody = pwd.getpwnam("nobody")
    os.chown(data, nobody.pw_uid, nobody.pw_gid)
    assert node_user.take_host_owner(accounts.pw, str(data), str(accounts.passwd),
                                     str(accounts.group)) is accounts.pw
    assert (accounts.passwd.read_bytes(), accounts.group.read_bytes()) == before
    assert f"which is nobody in the image: the node's user stays uid {accounts.pw.pw_uid}" in \
        capfd.readouterr().err


@as_root_with_the_node_user
def test_roots_group_is_never_the_node_users(tmp_path, node_user, accounts):
    os.chown(tmp_path / "data", 4242, 0)
    pw = node_user.take_host_owner(accounts.pw, str(tmp_path / "data"), str(accounts.passwd),
                                   str(accounts.group))
    assert (pw.pw_uid, pw.pw_gid) == (4242, accounts.pw.pw_gid)
    assert _entry(accounts.group, "agent")[2] == str(accounts.pw.pw_gid)


@as_root_with_the_node_user
def test_the_image_entrypoint_runs_the_command_as_the_host_user(tmp_path, node_user, accounts,
                                                               monkeypatch):
    """The whole start, the drop included; the image's real home left alone
    (its re-owning is the test above's)."""
    os.chown(tmp_path / "data", 4242, 4243)
    (tmp_path / "data" / "left-by-the-image").write_text("x")
    monkeypatch.setattr(node_user, "_rehome", lambda *a: None)
    out = _run_in_a_child(node_user, ["sh", "-c", "id -u; id -g"], 1).split()
    assert out == ["4242", "4243"]
    assert (tmp_path / "data" / "left-by-the-image").stat().st_uid == 4242


def _in_a_child(fn) -> str:
    """fn() in a forked child, which may drop root; what it returned."""
    r, w = os.pipe()
    pid = os.fork()
    if pid == 0:
        os.close(r)
        try:
            out = fn()
        except BaseException as e:      # the child reports, whatever happened
            out = f"raised {type(e).__name__}: {e}"
        os.write(w, out.encode())
        os._exit(0)
    os.close(w)
    with os.fdopen(r) as f:
        out = f.read()
    os.waitpid(pid, 0)
    return out


@as_root_with_the_node_user
def test_the_node_folders_are_handed_to_its_user(tmp_path, node_user):
    agent = pwd.getpwnam("agent")
    tree = tmp_path / "data"
    (tree / "tls").mkdir()
    key = tree / "tls" / "key.pem"
    key.write_text("k")
    key.chmod(0o600)
    shown_as_roots = tree / "node_identity"           # a Windows mount shows a folder as root's, 0777
    shown_as_roots.mkdir()
    shown_as_roots.chmod(0o777)
    link = tree / "link"
    link.symlink_to("/etc/hostname")
    assert node_user.hand_over(agent, (str(tree),), ()) == (4, 0)   # data, tls, the key, the folder
    assert {p.stat().st_uid for p in (tree, key, shown_as_roots)} == {agent.pw_uid}
    assert os.lstat(link).st_uid == 0
    assert node_user.hand_over(agent, (str(tree),), ()) == (0, 0)


@as_root_with_the_node_user
def test_a_filesystem_that_refuses_the_hand_over_is_counted_not_fatal(tmp_path, node_user,
                                                                     monkeypatch):
    agent = pwd.getpwnam("agent")
    (tmp_path / "data" / "x").write_text("x")

    def refuse(path, uid, gid, follow_symlinks=True):
        raise PermissionError(1, "Operation not permitted", path)

    monkeypatch.setattr(node_user.os, "chown", refuse)
    assert node_user.hand_over(agent, (str(tmp_path / "data"),), ()) == (0, 2)


@as_root_with_the_node_user
def test_a_path_the_filesystem_will_not_read_is_counted_not_fatal(tmp_path, node_user, monkeypatch):
    """A drvfs mount's passing EIO, or a file gone during the walk, must not
    end the container's start."""
    import errno
    agent = pwd.getpwnam("agent")
    data = tmp_path / "data"
    for name in ("unreadable", "fine"):
        (data / name).write_text("x")
    real = os.lstat

    def lstat(path, *a, **kw):
        if str(path).endswith("unreadable"):
            raise OSError(errno.EIO, "Input/output error", str(path))
        return real(path, *a, **kw)

    monkeypatch.setattr(node_user.os, "lstat", lstat)
    assert node_user.hand_over(agent, (str(data),), ()) == (2, 1)        # data, fine; unreadable


@as_root_with_the_node_user
def test_only_an_empty_folder_docker_made_as_roots_is_handed(tmp_path, node_user, capfd):
    """A folder another party writes in too (the host agent's requests
    folder) is handed only while Docker's: root's and empty. One a person
    owns, or root's holding files, is left, and said."""
    agent = pwd.getpwnam("agent")
    made_by_docker = tmp_path / "codex-made"
    the_owners = tmp_path / "codex-owned"
    roots_own = tmp_path / "codex-roots"
    for folder in (made_by_docker, the_owners, roots_own):
        folder.mkdir()
    os.chown(the_owners, 4242, 4242)                 # a host user other than the image's
    (roots_own / "config.toml").write_text("")
    folders = tuple(map(str, (made_by_docker, the_owners, roots_own)))
    assert node_user.hand_over(agent, (), folders) == (1, 0)
    assert [f.stat().st_uid for f in (made_by_docker, the_owners, roots_own)] == [agent.pw_uid, 4242, 0]
    said = capfd.readouterr().err
    assert f"{the_owners} is uid 4242's" in said and f"{roots_own} is uid 0's and holds files" in said


@as_root_with_the_node_user
def test_a_folder_the_filesystem_will_not_describe_or_list_is_counted_and_named(
        tmp_path, node_user, monkeypatch, capfd):
    import errno
    agent = pwd.getpwnam("agent")
    data = tmp_path / "data"
    (data / "mount").mkdir()
    (data / "listing").mkdir()
    real_statvfs, real_scandir = os.statvfs, os.scandir

    def statvfs(path):
        if str(path).endswith("mount"):
            raise OSError(errno.EIO, "Input/output error", str(path))
        return real_statvfs(path)

    def scandir(path="."):
        if str(path).endswith("listing"):
            raise OSError(errno.EIO, "Input/output error", str(path))
        return real_scandir(path)

    monkeypatch.setattr(node_user.os, "statvfs", statvfs)
    monkeypatch.setattr(node_user.os, "scandir", scandir)
    assert node_user.hand_over(agent, (str(data),), ()) == (1, 2)    # data alone
    assert f"not handed over: {data / 'mount'}: Input/output error" in capfd.readouterr().err


@as_root_with_the_node_user
def test_the_image_entrypoint_runs_the_command_as_the_node_user(tmp_path, node_user):
    agent = pwd.getpwnam("agent")
    out = _run_in_a_child(node_user, ["sh", "-c", 'id -u; id -G; echo "$HOME"'], 1).split()
    assert out == [str(agent.pw_uid), str(agent.pw_gid), agent.pw_dir]
    assert (tmp_path / "data").stat().st_uid == agent.pw_uid


@as_root_with_the_node_user
def test_after_the_drop_root_cannot_be_taken_back(node_user):
    def drop():
        node_user.become_node_user((), ())
        libc = ctypes.CDLL(None, use_errno=True)
        return " ".join([str(os.getuid()), str(os.geteuid()), ",".join(map(str, os.getgroups())),
                         str(node_user.can_become_root()), str(libc.prctl(3, 0, 0, 0, 0) == 1)])

    agent = pwd.getpwnam("agent")
    # the kernel's own rule leaves a process whose user changed undumpable (PR_GET_DUMPABLE != 1)
    assert _in_a_child(drop) == f"{agent.pw_uid} {agent.pw_uid} {agent.pw_gid} False False"


def _run_in_a_child(node_user, argv, fd) -> str:
    """node_user.main(argv) in a forked child, file descriptor `fd` of it (1
    or 2) read back."""
    r, w = os.pipe()
    pid = os.fork()
    if pid == 0:
        os.close(r)
        os.dup2(w, fd)
        try:
            node_user.main(argv)
        finally:
            os._exit(1)
    os.close(w)
    with os.fdopen(r) as f:
        out = f.read()
    os.waitpid(pid, 0)
    return out


@as_root_with_the_node_user
def test_a_cache_mounted_in_roots_home_is_named_and_the_drop_still_happens(node_user, monkeypatch,
                                                                           capfd):
    monkeypatch.setattr(node_user.os.path, "ismount", lambda p: p == "/root/.cache")
    _run_in_a_child(node_user, ["true"], 1)
    said = capfd.readouterr().err
    assert "mounted at /root/.cache" in said and "running as agent" in said


def test_the_image_entrypoint_imports_nothing_but_the_standard_library():
    import ast
    tree = ast.parse((REPO / "backend" / "node_user.py").read_text(encoding="utf-8"))
    imported = {a.name.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.Import)
                for a in n.names}
    imported |= {n.module.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
    assert imported <= set(sys.stdlib_module_names)


@pytest.mark.skipif(sys.platform != "linux", reason="prctl is Linux's")
def test_a_server_process_is_undumpable_for_its_own_user():
    from serving import undumpable

    def check():
        undumpable()
        return str(ctypes.CDLL(None).prctl(3, 0, 0, 0, 0))

    assert _in_a_child(check) == "0"


# -- the API secret ------------------------------------------------------------------

def test_the_api_secret_is_the_owners_alone(tmp_path):
    import auth_hmac
    path = tmp_path / "node_identity" / ".api_secret"
    secret = auth_hmac.ensure_secret(path)
    assert len(secret) == 43
    assert path.stat().st_mode & 0o777 == 0o600 and path.parent.stat().st_mode & 0o777 == 0o700
    path.chmod(0o644)                       # as an earlier release, or a restore, left it
    path.parent.chmod(0o755)
    assert auth_hmac.ensure_secret(path) == secret
    assert path.stat().st_mode & 0o777 == 0o600 and path.parent.stat().st_mode & 0o777 == 0o700


@as_root_with_the_node_user
def test_a_root_backend_gives_the_secret_to_the_user_its_agents_run_as(tmp_path):
    import auth_hmac
    agent = pwd.getpwnam("agent")
    path = tmp_path / "node_identity" / ".api_secret"
    auth_hmac.ensure_secret(path)
    assert (path.stat().st_uid, path.parent.stat().st_uid) == (agent.pw_uid, agent.pw_uid)
    assert path.stat().st_mode & 0o777 == 0o600


@as_root_with_the_node_user
def test_a_secret_another_user_owns_is_read_as_it_is(tmp_path):
    """A secret an earlier root backend wrote on a mount that kept it root's:
    the node's user reads it, its mode stays, the start goes on."""
    import shutil
    import tempfile
    import auth_hmac
    base = Path(tempfile.mkdtemp(prefix="sautium-test-identity-"))
    base.chmod(0o755)
    folder = base / "node_identity"
    folder.mkdir()
    folder.chmod(0o777)
    path = folder / ".api_secret"
    path.write_text("an-earlier-secret-of-forty-three-characters")
    path.chmod(0o644)
    agent = pwd.getpwnam("agent")

    def as_the_node_user():
        os.setgid(agent.pw_gid)
        os.setuid(agent.pw_uid)
        return auth_hmac.ensure_secret(path).decode()

    try:
        assert _in_a_child(as_the_node_user) == "an-earlier-secret-of-forty-three-characters"
        assert (path.stat().st_uid, path.stat().st_mode & 0o777) == (0, 0o644)
    finally:
        shutil.rmtree(base)


def test_an_empty_secret_file_counts_as_none(tmp_path):
    import auth_hmac
    path = tmp_path / "node_identity" / ".api_secret"
    path.parent.mkdir()
    path.write_text("")                     # a write that did not finish
    assert len(auth_hmac.ensure_secret(path)) == 43
    assert not list(path.parent.glob("*.part"))


@as_root_with_the_node_user
def test_a_folder_another_user_owns_keeps_its_mode_and_the_start_goes_on(tmp_path):
    """A mount that refused the hand-over: the node's user cannot set the
    folder's mode, and the secret is still made and read."""
    import shutil
    import tempfile
    import auth_hmac
    base = Path(tempfile.mkdtemp(prefix="sautium-test-identity-"))   # pytest's are root's alone
    base.chmod(0o755)
    folder = base / "node_identity"
    folder.mkdir()
    folder.chmod(0o777)                     # root's, as such a mount shows it
    path = folder / ".api_secret"
    agent = pwd.getpwnam("agent")

    def as_the_node_user():
        os.setgid(agent.pw_gid)
        os.setuid(agent.pw_uid)
        return auth_hmac.ensure_secret(path).decode()

    try:
        secret = _in_a_child(as_the_node_user)
        assert len(secret) == 43 and path.read_text() == secret
        assert path.stat().st_mode & 0o777 == 0o600 and folder.stat().st_mode & 0o777 == 0o777
    finally:
        shutil.rmtree(base)


@pytest.mark.skipif(sys.platform != "linux", reason="the Docker node is Linux")
def test_a_cli_put_under_the_node_users_home_never_stands_in_for_the_images(tmp_path, monkeypatch):
    """The per-user prefixes are the launcher's; on the Docker node they would
    sit in the home its own agents can write."""
    import platform
    import claude_code
    import codex_cli
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    arch = "arm64" if platform.machine().lower() in ("arm64", "aarch64") else "x64"
    planted = [tmp_path / "Sautium" / "claude-prefix" / "node_modules" / "@anthropic-ai" / "claude-code"
               / "bin" / "claude.exe",
               tmp_path / "Sautium" / "codex-prefix" / "node_modules" / "@openai" / f"codex-linux-{arch}"
               / "vendor" / "x86_64-unknown-linux-musl" / "bin" / "codex"]
    for path in planted:
        path.parent.mkdir(parents=True)
        path.write_text("#!/bin/sh\n")
        path.chmod(0o755)
    assert claude_code.get_claude_executable() not in planted
    assert codex_cli.get_codex_executable() not in planted


def test_the_folder_whose_owner_the_node_user_takes_is_the_clones_code():
    """/app is the checkout's backend/, which a clone makes its user's, and
    no keep-file ships in a folder the node writes (a folder Docker makes
    for a bind is root's, and says nothing)."""
    import node_user
    assert node_user.HOST_OWNED == "/app"
    assert not (REPO / "backend" / "data" / ".gitkeep").exists()
    compose = (REPO / "docker-compose.pi.yml").read_text(encoding="utf-8")
    assert "      - ./backend:/app:ro\n" in compose and "      - ./backend/data:/app/data\n" in compose


@as_root_with_the_node_user
def test_a_tree_or_folder_reached_through_a_link_is_not_handed(tmp_path, node_user, capfd):
    """./data/claude holds projects/ and ./backend holds data/, both the node
    user's to write: one swapped for a link would aim root's chown at
    whatever the link names."""
    agent = pwd.getpwnam("agent")
    target = tmp_path / "roots"
    target.mkdir()
    (target / "entrypoint.py").write_text("x")
    link = tmp_path / "projects"
    link.symlink_to(target)
    assert node_user.hand_over(agent, (str(link),), (str(link),)) == (0, 0)
    assert (target / "entrypoint.py").stat().st_uid == 0 and target.stat().st_uid == 0
    assert f"not handed over: {link} is reached through a link" in capfd.readouterr().err
    through = tmp_path / "via"
    through.symlink_to(tmp_path)                         # a link in an earlier component
    assert node_user.hand_over(agent, (str(through / "roots"),), ()) == (0, 0)
    assert target.stat().st_uid == 0


@as_root_with_the_node_user
def test_a_data_folder_reached_through_a_link_leaves_the_uid(tmp_path, node_user, accounts, capfd):
    owned = tmp_path / "someones"
    owned.mkdir()
    os.chown(owned, 4242, 4243)
    link = tmp_path / "linked-data"
    link.symlink_to(owned)
    assert node_user.take_host_owner(accounts.pw, str(link), str(accounts.passwd),
                                     str(accounts.group)) is accounts.pw
    assert "reached through a link" in capfd.readouterr().err


@as_root_with_the_node_user
def test_a_group_of_the_images_own_is_never_the_node_users(tmp_path, node_user, accounts):
    import grp
    shadow = grp.getgrnam("shadow").gr_gid
    os.chown(tmp_path / "data", 4242, shadow)
    pw = node_user.take_host_owner(accounts.pw, str(tmp_path / "data"), str(accounts.passwd),
                                   str(accounts.group))
    assert (pw.pw_uid, pw.pw_gid) == (4242, accounts.pw.pw_gid)
    assert _entry(accounts.passwd, "agent")[3] == str(accounts.pw.pw_gid)


def test_the_mount_points_are_read_with_their_escapes(tmp_path, monkeypatch):
    import node_user
    info = tmp_path / "mountinfo"
    info.write_text(
        "22 1 0:21 / / rw,relatime - overlay overlay rw\n"
        "31 22 8:2 /srv/a\\040b /home/agent/.cache rw,relatime - ext4 /dev/sda2 rw\n"
        "32 22 8:2 /x /home/agent/my\\134dir rw - ext4 /dev/sda2 rw\n")
    monkeypatch.setattr(node_user, "MOUNTINFO", str(info))
    assert node_user._mount_points() == {"/", "/home/agent/.cache", "/home/agent/my\\dir"}


@as_root_with_the_node_user
def test_a_data_folder_the_entrypoint_cannot_read_says_why_the_uid_stays(tmp_path, node_user,
                                                                        accounts, monkeypatch, capfd):
    import errno
    data = str(tmp_path / "data")
    real = os.lstat

    def lstat(path, *a, **kw):
        if str(path) == data:
            raise OSError(errno.EIO, "Input/output error", data)
        return real(path, *a, **kw)

    monkeypatch.setattr(node_user.os, "lstat", lstat)
    assert node_user.take_host_owner(accounts.pw, data, str(accounts.passwd),
                                     str(accounts.group)) is accounts.pw
    assert (f"{data} could not be read (Input/output error): the node's user stays uid "
            f"{accounts.pw.pw_uid}") in capfd.readouterr().err


def test_in_docker_the_cli_reads_no_settings_from_the_nodes_own_claude_folder(monkeypatch):
    """A Docker node's ~/.claude is a folder the node writes: a hook a node
    that was controlled once left in its settings.json ran at every chat
    turn of every release after (measured with the image's CLI: the hook ran
    without these flags and not with them). A launcher's CLI reads its
    person's own settings as before."""
    monkeypatch.setattr(claude_code_runner, "IN_DOCKER", True)
    for tools in (claude_code_runner.LIBRARY, claude_code_runner.RESEARCH, claude_code_runner.TEXT):
        flags = claude_code_runner._tool_flags(tools)
        assert flags[flags.index("--setting-sources") + 1] == "project"
        assert flags[flags.index("--settings") + 1] == '{"disableAllHooks": true}'
    monkeypatch.setattr(claude_code_runner, "IN_DOCKER", False)
    assert "--setting-sources" not in claude_code_runner._tool_flags(claude_code_runner.LIBRARY)


def test_a_data_folder_mounted_from_where_a_link_pointed_is_refused(tmp_path, monkeypatch):
    """Until 2026-11-15 backend/data was inside the node's writable /app: a
    node of then could make it a link, and Docker follows a link in a bind's
    source (measured: the kernel records the link's target as the bind's
    root). The entrypoint refuses to hand over a /app/data mounted from
    anywhere but a checkout's backend/data."""
    import node_user
    mountinfo = tmp_path / "mountinfo"
    line = "100 50 0:42 {root} /app/data rw,relatime - ext4 /dev/sda1 rw\n"
    monkeypatch.setattr(node_user, "MOUNTINFO", str(mountinfo))
    for root, good in (("/home/pi/sautium/backend/data", True), ("/ai/djai/backend/data", True),
                       ("/etc", False), ("/home/pi", False), ("/home/pi/sautium/backend/data\\040x", False)):
        mountinfo.write_text(line.format(root=root))
        assert node_user.data_bind_is_the_checkouts() is good, root
    mountinfo.write_text("100 50 0:42 / /app rw - ext4 /dev/sda1 rw\n")
    assert node_user.data_bind_is_the_checkouts()


def test_a_codex_chat_whose_thread_the_cli_does_not_hold_goes_on_as_a_new_thread(monkeypatch):
    """B-94 gives a Docker node its own ~/.codex: a chat begun when the
    host's was mounted names a thread the CLI no longer holds, and `exec
    resume` fails before anything is said (measured, codex 0.149). The turn
    goes on as a new thread, whose id the chat then keeps; any other failure,
    or one after the answer began, is the answer as it was."""
    import contextlib
    import codex_runner
    from providers.base import StreamDone, TextDelta
    calls = []

    def stream(message, thread_id, resume, model):
        calls.append((thread_id, resume))
        if resume:
            yield StreamDone(model="m", provider="codex",
                             error="thread/resume failed: no rollout found for thread id 0199 (code -32600)")
        else:
            yield TextDelta(text="hello")
            yield StreamDone(model="m", provider="codex", agent_session_id="new-thread")

    monkeypatch.setattr(codex_runner, "library_turn", contextlib.nullcontext)
    monkeypatch.setattr(codex_runner, "_codex_stream", stream)
    events = list(codex_runner.call_codex_stream("hi", thread_id="0199", resume=True))
    assert calls == [("0199", True), (None, False)]
    assert events[-1].agent_session_id == "new-thread" and events[-1].error is None

    def other(message, thread_id, resume, model):
        calls.append((thread_id, resume))
        yield StreamDone(model="m", provider="codex", error="Codex timed out after 150s")

    calls.clear()
    monkeypatch.setattr(codex_runner, "_codex_stream", other)
    events = list(codex_runner.call_codex_stream("hi", thread_id="0199", resume=True))
    assert calls == [("0199", True)] and events[-1].error == "Codex timed out after 150s"


def test_codex_runs_no_hook_from_the_nodes_own_folder():
    """CODEX_HOME is a folder the node writes (B-94); codex 0.149 runs the
    hooks a file there names, trusted by a record kept there too."""
    import codex_runner
    flags = codex_runner._base_flags("m", "low", with_mcp=False, instructions=Path("/x"))
    assert [flags[i + 1] for i, f in enumerate(flags) if f == "--disable"].count("hooks") == 1
    assert "--ignore-user-config" in flags and "--ignore-rules" in flags


def test_an_agents_folder_mounted_from_anywhere_but_the_nodes_data_is_never_handed(tmp_path, monkeypatch, capfd):
    """Since B-94 the agents' folders are trees of the node's: walked and
    re-owned at each start. Mounted from a person's own ~/.claude (an
    override, a link standing in for ./data/claude), they are left as they
    are, and said."""
    import node_user
    claude, codex = tmp_path / "agent" / ".claude", tmp_path / "agent" / ".codex"
    for folder in (claude, codex):
        (folder / "sub").mkdir(parents=True)
        (folder / "sub" / "f").write_text("x")
    mountinfo = tmp_path / "mountinfo"
    mountinfo.write_text(f"1 0 0:1 /home/pi/.claude {claude} rw - ext4 /dev/sda1 rw\n"
                         f"2 0 0:1 /srv/sautium/data/codex {codex} rw - ext4 /dev/sda1 rw\n")
    monkeypatch.setattr(node_user, "MOUNTINFO", str(mountinfo))
    monkeypatch.setattr(node_user, "AGENT_HOMES", (str(claude), str(codex)))
    chowned = []
    monkeypatch.setattr(node_user.os, "chown", lambda path, *a, **k: chowned.append(str(path)))
    pw = pwd.struct_passwd(("agent", "x", os.getuid() + 1, os.getgid(), "", str(tmp_path), "/bin/sh"))
    node_user.hand_over(pw, (str(claude), str(codex)), ())
    assert chowned and all(p.startswith(str(codex)) for p in chowned)
    assert f"{claude} is mounted from /home/pi/.claude" in capfd.readouterr().err


def test_a_codex_failure_after_the_answer_began_is_the_answer_as_it_was(monkeypatch):
    import contextlib
    import codex_runner
    from providers.base import StreamDone, TextDelta
    calls = []

    def stream(message, thread_id, resume, model):
        calls.append(resume)
        yield TextDelta(text="part of an answer")
        yield StreamDone(model="m", provider="codex",
                         error="thread/resume failed: no rollout found for thread id 0199")

    monkeypatch.setattr(codex_runner, "library_turn", contextlib.nullcontext)
    monkeypatch.setattr(codex_runner, "_codex_stream", stream)
    events = list(codex_runner.call_codex_stream("hi", thread_id="0199", resume=True))
    assert calls == [True] and "no rollout found" in events[-1].error
    # the CLI's log chatter naming a "session not found" is no unknown thread
    assert not codex_runner._UNKNOWN_THREAD.search("WARN mcp: session not found, retrying")


def test_the_agent_folder_guard_inside_an_accepted_mount(tmp_path, monkeypatch, capfd):
    """An accepted ./data/claude with ./data/claude_sessions over projects/ is
    walked whole; a folder or a file mounted in it from elsewhere is left;
    a node home mounted from elsewhere whole leaves every agent folder."""
    import node_user
    claude = tmp_path / "agent" / ".claude"
    for sub in ("projects", "plugins", "x"):
        (claude / sub).mkdir(parents=True)
        (claude / sub / "f").write_text("x")
    (claude / ".credentials.json").write_text("{}")
    mountinfo = tmp_path / "mountinfo"
    lines = [f"1 0 0:1 /srv/sautium/data/claude {claude} rw - ext4 /dev/sda1 rw",
             f"2 0 0:1 /srv/sautium/data/claude_sessions {claude}/projects rw - ext4 /dev/sda1 rw",
             f"3 0 0:1 /home/pi/.claude/plugins {claude}/plugins rw - ext4 /dev/sda1 rw",
             f"4 0 0:1 /home/pi/.claude/.credentials.json {claude}/.credentials.json rw - ext4 /dev/sda1 rw"]
    mountinfo.write_text("\n".join(lines) + "\n")
    monkeypatch.setattr(node_user, "MOUNTINFO", str(mountinfo))
    monkeypatch.setattr(node_user, "AGENT_HOMES", (str(claude), str(tmp_path / "agent" / ".codex")))
    chowned = []
    monkeypatch.setattr(node_user.os, "chown", lambda path, *a, **k: chowned.append(str(path)))
    pw = pwd.struct_passwd(("agent", "x", os.getuid() + 1, os.getgid(), "", str(tmp_path), "/bin/sh"))
    node_user.hand_over(pw, (str(claude),), ())
    assert str(claude / "projects" / "f") in chowned and str(claude / "x" / "f") in chowned
    assert not [p for p in chowned if "/plugins" in p or p.endswith(".credentials.json")]
    chowned.clear()
    mountinfo.write_text(f"9 0 0:1 /home/pi {tmp_path / 'agent'} rw - ext4 /dev/sda1 rw\n")
    node_user.hand_over(pw, (str(claude),), ())
    assert chowned == [] and "lies in a home mounted from /home/pi" in capfd.readouterr().err
