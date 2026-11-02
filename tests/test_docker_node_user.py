"""The Docker node runs as its own user (backend/entrypoint.py): the
container starts as root only to hand the node's folders to that user, then
drops root before anything else runs. An agent CLI never runs as root and
carries none of the node's secrets in its environment
(claude_code_runner.spawn_kwargs). The drops are real ones, so those tests
run where the suite runs as root, in the backend's image."""

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
           "ANTHROPIC_API_KEY": "a"}


def test_an_agent_child_carries_none_of_the_nodes_secrets():
    env = {"PATH": "/usr/bin", "LANG": "C.UTF-8", "CODEX_HOME": "/somewhere", **SECRETS}
    kwargs = claude_code_runner.spawn_kwargs(env)
    assert set(kwargs["env"]) & set(SECRETS) == set()
    assert {"PATH", "LANG", "CODEX_HOME"} <= set(kwargs["env"])


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
    kwargs = codex_runner._spawn_kwargs(dict(SECRETS, PATH="/usr/bin"))
    assert set(kwargs["env"]) & set(SECRETS) == set() and kwargs["cwd"] == "/tmp"


# -- the image's entrypoint (backend/node_user.py) ----------------------------------

@pytest.fixture
def node_user(tmp_path, monkeypatch):
    """The image's entrypoint, its folders the test's own: run for real, as
    root, it hands them over and drops root, so a child process runs it."""
    import node_user
    monkeypatch.setattr(node_user, "NODE_TREES", (str(tmp_path / "data"),))
    monkeypatch.setattr(node_user, "NODE_FOLDERS", ())
    (tmp_path / "data").mkdir()
    return node_user


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
