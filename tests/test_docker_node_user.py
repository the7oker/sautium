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


# -- the entrypoint ------------------------------------------------------------------

@pytest.fixture
def entrypoint(monkeypatch):
    import entrypoint
    return entrypoint


@as_root_with_the_node_user
def test_the_node_folders_are_handed_to_its_user_where_it_could_not_write(tmp_path, entrypoint):
    agent = pwd.getpwnam("agent")
    tree = tmp_path / "data"
    (tree / "tls").mkdir(parents=True)
    root_file = tree / "tls" / "key.pem"
    root_file.write_text("k")
    root_file.chmod(0o600)
    shared = tree / "shared.txt"
    shared.write_text("s")
    shared.chmod(0o666)                 # writable by all: a Docker Desktop mount of a host folder
    link = tree / "link"
    link.symlink_to("/etc/hostname")
    handed = entrypoint._hand_over(agent, (str(tree),), ())
    assert handed == 3                  # data, data/tls, the key
    assert root_file.stat().st_uid == agent.pw_uid and tree.stat().st_uid == agent.pw_uid
    assert shared.stat().st_uid == 0 and os.lstat(link).st_uid == 0
    assert entrypoint._hand_over(agent, (str(tree),), ()) == 0


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
def test_the_entrypoint_drops_root_for_good_before_the_backend_starts(tmp_path, monkeypatch,
                                                                      entrypoint):
    monkeypatch.setattr(entrypoint, "_NODE_TREES", (str(tmp_path),))
    monkeypatch.setattr(entrypoint, "_NODE_FOLDERS", ())

    def drop():
        entrypoint._become_node_user()
        libc = ctypes.CDLL(None, use_errno=True)
        dumpable = libc.prctl(3, 0, 0, 0, 0)          # PR_GET_DUMPABLE
        return " ".join([str(os.getuid()), str(os.geteuid()), ",".join(map(str, os.getgroups())),
                         os.environ["HOME"], str(entrypoint._can_become_root()), str(dumpable)])

    agent = pwd.getpwnam("agent")
    assert _in_a_child(drop) == (f"{agent.pw_uid} {agent.pw_uid} {agent.pw_gid} "
                                 f"{agent.pw_dir} False 0")
    assert tmp_path.stat().st_uid == agent.pw_uid


@as_root_with_the_node_user
def test_a_container_from_an_older_compose_file_reaches_its_cache_as_the_node_user(
        monkeypatch, entrypoint):
    """Its model cache is mounted in root's home: the node's user reaches
    that mount, so nothing is fetched again before the container is created
    afresh. A folder of root's alone stands for root's home (pytest's own
    are under one)."""
    import shutil
    import tempfile
    tmp_path = Path(tempfile.mkdtemp(prefix="sautium-test-root-"))
    root_cache = tmp_path / "root-cache"
    (root_cache / "huggingface").mkdir(parents=True)
    (root_cache / "huggingface" / "model.bin").write_bytes(b"weights")
    home_cache = tmp_path / "home-cache"
    home_cache.mkdir()                                  # the image's own, empty
    monkeypatch.setattr(entrypoint, "_ROOT_CACHE", str(root_cache))
    monkeypatch.setattr(entrypoint, "_NODE_CACHE", str(home_cache))
    monkeypatch.setattr(entrypoint, "_NODE_TREES", ())
    monkeypatch.setattr(entrypoint, "_NODE_FOLDERS", ())
    monkeypatch.setattr(entrypoint.os.path, "ismount", lambda p: p == str(root_cache))
    real_chmod = os.chmod
    monkeypatch.setattr(entrypoint.os, "chmod", lambda p, m: real_chmod(tmp_path, m)
                        if p == "/root" else real_chmod(p, m))

    def drop():
        entrypoint._become_node_user()
        return f"{os.geteuid()} {(home_cache / 'huggingface' / 'model.bin').read_bytes().decode()}"

    agent = pwd.getpwnam("agent")
    try:
        assert _in_a_child(drop) == f"{agent.pw_uid} weights"
        assert os.readlink(home_cache) == str(root_cache)
        assert (root_cache / "huggingface" / "model.bin").stat().st_uid == agent.pw_uid
    finally:
        shutil.rmtree(tmp_path)


# -- the API secret ------------------------------------------------------------------

def test_the_api_secret_is_the_owners_alone(tmp_path, monkeypatch):
    import auth_hmac
    monkeypatch.setattr(auth_hmac.os, "geteuid", lambda: 1000)
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
