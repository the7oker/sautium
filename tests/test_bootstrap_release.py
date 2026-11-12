"""The bootstraps' first checkout is a signed release (desktop/windows/
bootstrap.py, desktop/macos/bootstrap.py): the payload's own updater, run on
the runtime, checks out the newest release that verifies against the
payload's own desktop/release_signers; none → the bundled copy, as with no
network. The payload's packages go in before that checkout, since the
signature check needs them. Each bootstrap loaded as the file it is, on this
interpreter, against a bare origin with throwaway keys."""

import importlib.util
import shutil
import subprocess
import sys
import threading
import types
from pathlib import Path

import pytest

from tests.release_signing import Key, push_signed_tag

DESKTOP = Path(__file__).resolve().parent.parent / "desktop"


def git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True,
                          capture_output=True, text=True).stdout.strip()


@pytest.fixture
def origin(tmp_path, monkeypatch):
    # Not from the repository's root, whose own desktop/ would hide a
    # bootstrap that forgot to run the payload's code from the payload.
    monkeypatch.chdir(tmp_path)
    for key, value in {"GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1",
                       "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                       "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}.items():
        monkeypatch.setenv(key, value)
    bare = tmp_path / "origin.git"
    git(tmp_path, "init", "-q", "--bare", "-b", "main", str(bare))
    git(bare, "config", "uploadpack.allowFilter", "true")
    seed = tmp_path / "seed"
    git(tmp_path, "clone", "-q", str(bare), str(seed))
    (seed / "desktop").mkdir()
    (seed / "desktop" / "requirements.txt").write_text("released\n")
    (seed / "desktop" / "build_common.py").write_text('VERSION = "0.2.0"\n')
    git(seed, "add", ".")
    git(seed, "commit", "-qm", "release")
    git(seed, "push", "-q", "origin", "HEAD:main")
    return types.SimpleNamespace(url=str(bare), seed=seed, key=Key(sk=True),
                                 tip=git(seed, "rev-parse", "HEAD"))


def publish(origin, name, key=None, lightweight=False):
    if lightweight:
        git(origin.seed, "tag", name)
    else:
        push_signed_tag(origin.seed, key or origin.key, origin.tip, name)
    git(origin.seed, "push", "-q", "origin", f"refs/tags/{name}")


def payload(tmp_path, key) -> Path:
    """What an installer carries of the tree: the updater and its verifier,
    and the list of release keys."""
    root = tmp_path / "payload"
    (root / "desktop").mkdir(parents=True)
    for name in ("__init__.py", "updater.py", "release_verify.py", "requirements.txt"):
        shutil.copy2(DESKTOP / name, root / "desktop" / name)
    (root / "desktop" / "release_signers").write_text(key.line() + "\n")
    installer(root, "0.2.0")
    return root


def installer(root: Path, version: str) -> None:
    """Make the payload another installer's: its stated version and its build id."""
    (root / "desktop" / "build_common.py").write_text(f'VERSION = "{version}"\n')
    (root / ".sautium_build").write_text(f"{version}+bundle\n")


def load(path: Path, name: str, monkeypatch):
    if importlib.util.find_spec("tkinter") is None:
        monkeypatch.setitem(sys.modules, "tkinter", types.SimpleNamespace(Canvas=object))
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def windows(tmp_path, monkeypatch, origin):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    monkeypatch.setattr(subprocess, "CREATE_NO_WINDOW", 0, raising=False)
    boot = load(DESKTOP / "windows" / "bootstrap.py", "windows_bootstrap", monkeypatch)
    root = payload(tmp_path, origin.key)
    monkeypatch.setattr(boot, "REPO_URL", origin.url)
    monkeypatch.setattr(boot, "PAYLOAD", root)
    monkeypatch.setattr(boot, "PAYLOAD_SIGNERS", root / "desktop" / "release_signers")
    monkeypatch.setattr(boot, "RUNTIME_PYTHON", Path(sys.executable))
    monkeypatch.setattr(boot, "GIT", shutil.which("git"))
    if sys.version_info < (3, 12):      # the installer's runtime is 3.12 (rmtree's onexc)
        monkeypatch.setattr(boot, "remove_tree", lambda path: shutil.rmtree(path, ignore_errors=True))
    return boot


@pytest.fixture
def macos(tmp_path, monkeypatch, origin):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "share"))
    boot = load(DESKTOP / "macos" / "bootstrap.py", "macos_bootstrap", monkeypatch)
    root = payload(tmp_path, origin.key)
    monkeypatch.setattr(boot, "REPO_URL", origin.url)
    monkeypatch.setattr(boot, "PAYLOAD", root)
    monkeypatch.setattr(boot, "PAYLOAD_SIGNERS", root / "desktop" / "release_signers")
    monkeypatch.setattr(boot, "RUNTIME_PYTHON", Path(sys.executable))
    return boot


@pytest.fixture(params=["windows", "macos"])
def bootstrap(request):
    return request.getfixturevalue(request.param)


def test_the_first_checkout_is_the_newest_verified_release(bootstrap, origin):
    publish(origin, "v0.2.0")
    publish(origin, "v0.3.0", key=Key())                    # a key the payload does not list
    (bootstrap.APP_DIR / ".sautium_build").parent.mkdir(parents=True, exist_ok=True)
    assert bootstrap.clone_tree(lambda _: None) is True
    assert git(bootstrap.APP_DIR, "rev-parse", "HEAD") == origin.tip
    assert (bootstrap.APP_DIR / "desktop" / "requirements.txt").read_text() == "released\n"


def test_the_payloads_code_runs_without_writing_into_the_payload(bootstrap, origin, monkeypatch):
    monkeypatch.delenv("PYTHONDONTWRITEBYTECODE", raising=False)
    publish(origin, "v0.2.0")
    assert bootstrap.clone_tree(lambda _: None) is True
    assert list(bootstrap.PAYLOAD.rglob("__pycache__")) == []


def _clone_at(bootstrap, origin, name):
    publish(origin, name)
    bootstrap.APP_DIR.parent.mkdir(parents=True, exist_ok=True)
    assert bootstrap.clone_tree(lambda _: None) is True
    bootstrap.PAYLOAD_SEEN.write_text(bootstrap.bundle_build_id())


def _new_release(origin, name, orphan=False):
    if orphan:
        git(origin.seed, "checkout", "-q", "--orphan", "rewritten")
    (origin.seed / "desktop" / "requirements.txt").write_text(f"{name}\n")
    (origin.seed / "desktop" / "build_common.py").write_text(f'VERSION = "{name[1:]}"\n')
    git(origin.seed, "add", ".")
    git(origin.seed, "commit", "-qm", name)
    origin.tip = git(origin.seed, "rev-parse", "HEAD")
    publish(origin, name)


def test_a_reinstall_over_a_clone_moves_it_past_a_rewritten_history(bootstrap, origin):
    _clone_at(bootstrap, origin, "v0.2.0")
    _new_release(origin, "v0.3.0", orphan=True)
    installer(bootstrap.PAYLOAD, "0.3.0")                      # the next installer
    bootstrap.sync_payload(lambda _: None)
    assert git(bootstrap.APP_DIR, "rev-parse", "HEAD") == origin.tip
    assert bootstrap.PAYLOAD_SEEN.read_text().strip() == "0.3.0+bundle"


def test_the_bundle_that_reconciled_a_clone_never_runs_over_it_again(bootstrap, origin, monkeypatch):
    """Round 2's A: the worker also runs over a clone for a stale package
    marker or a relaunch after an update — the same bundle must not move it."""
    _clone_at(bootstrap, origin, "v0.2.0")
    _new_release(origin, "v0.3.0", orphan=True)
    ran = []
    monkeypatch.setattr(bootstrap, "install_release", lambda report: ran.append(1))
    bootstrap.sync_payload(lambda _: None)
    assert ran == []


def test_an_older_installer_never_moves_a_clone(bootstrap, origin):
    _clone_at(bootstrap, origin, "v0.2.0")
    _new_release(origin, "v0.3.0")
    git(bootstrap.APP_DIR, "fetch", "-q", "origin", "refs/tags/v0.3.0")
    git(bootstrap.APP_DIR, "checkout", "-q", "--force", "-B", "main", "FETCH_HEAD")   # its updater took v0.3.0
    held = git(bootstrap.APP_DIR, "rev-parse", "HEAD")
    _new_release(origin, "v0.4.0", orphan=True)                               # a rewrite above it
    installer(bootstrap.PAYLOAD, "0.2.5")                                      # an older installer run again
    bootstrap.sync_payload(lambda _: None)
    assert git(bootstrap.APP_DIR, "rev-parse", "HEAD") == held
    assert bootstrap.PAYLOAD_SEEN.read_text().strip() == "0.2.5+bundle"


def test_a_failed_reinstall_is_tried_again(bootstrap, origin, monkeypatch):
    _clone_at(bootstrap, origin, "v0.2.0")
    installer(bootstrap.PAYLOAD, "0.3.0")
    def failing(report):
        raise RuntimeError("python failed (exit 1)")
    monkeypatch.setattr(bootstrap, "install_release", failing)
    bootstrap.sync_payload(lambda _: None)
    assert bootstrap.PAYLOAD_SEEN.read_text().strip() == "0.2.0+bundle"


def test_a_fresh_clone_records_its_bundle(bootstrap, origin):
    publish(origin, "v0.2.0")
    bootstrap.sync_payload(lambda _: None)
    assert bootstrap.tree_is_clone()
    assert bootstrap.PAYLOAD_SEEN.read_text().strip() == "0.2.0+bundle"


def _set_up(bootstrap, monkeypatch) -> bool:
    """Whether the bootstrap would launch at once, its runtime and packages
    taken as in place."""
    if hasattr(bootstrap, "runtime_current"):
        monkeypatch.setattr(bootstrap, "runtime_current", lambda: True)
        bootstrap.DEPS_MARKER.write_text(bootstrap.requirements_hash())
        return bootstrap.is_installed()
    return bootstrap.tree_current()


def test_a_clone_is_set_up_for_the_bundle_that_reconciled_it(bootstrap, origin, monkeypatch):
    _clone_at(bootstrap, origin, "v0.2.0")
    assert _set_up(bootstrap, monkeypatch)
    installer(bootstrap.PAYLOAD, "0.3.0")
    assert not _set_up(bootstrap, monkeypatch)


def test_a_reinstall_that_finds_nothing_newer_keeps_the_clone(bootstrap, origin):
    _clone_at(bootstrap, origin, "v0.2.0")
    held = git(bootstrap.APP_DIR, "rev-parse", "HEAD")
    (bootstrap.APP_DIR / "pgsql").mkdir()
    installer(bootstrap.PAYLOAD, "0.3.0")
    git(origin.seed, "push", "-q", "origin", ":refs/tags/v0.2.0")
    bootstrap.sync_payload(lambda _: None)
    assert git(bootstrap.APP_DIR, "rev-parse", "HEAD") == held
    assert (bootstrap.APP_DIR / "pgsql").is_dir()
    assert bootstrap.PAYLOAD_SEEN.read_text().strip() == "0.3.0+bundle"


def test_an_interrupted_first_checkout_is_no_clone(bootstrap):
    (bootstrap.APP_DIR / ".git").mkdir(parents=True)
    assert not bootstrap.tree_is_clone()


def test_no_signed_release_falls_back_to_the_bundled_copy(bootstrap, origin):
    publish(origin, "v0.2.0", lightweight=True)
    assert bootstrap.clone_tree(lambda _: None) is False
    assert not (bootstrap.APP_DIR / ".git").exists()


def test_the_windows_checkout_leaves_the_launchers_downloads_beside_it(windows, origin):
    publish(origin, "v0.2.0")
    windows.APP_DIR.mkdir(parents=True)
    (windows.APP_DIR / "pgsql").mkdir()
    (windows.APP_DIR / ".sautium_build").write_text("0.1.1+bundle\n")
    assert windows.clone_tree(lambda _: None) is True
    assert (windows.APP_DIR / "pgsql").is_dir()
    assert not (windows.APP_DIR / ".sautium_build").exists()


class _Window:
    def __init__(self):
        self.posts = []

    def post(self, kind, payload=None):
        self.posts.append(kind)


def _run_worker(boot, monkeypatch, calls):
    monkeypatch.setattr(boot, "install_deps", lambda report, tree=boot.APP_DIR: calls.append(("deps", tree)))
    monkeypatch.setattr(boot, "sync_payload", lambda report: calls.append(("sync", None)))
    monkeypatch.setattr(boot, "install_runtime", lambda report: calls.append(("runtime", None)), raising=False)
    monkeypatch.setattr(boot, "find_brew", lambda: "/opt/homebrew/bin/brew", raising=False)
    threads = []
    real = threading.Thread
    monkeypatch.setattr(boot.threading, "Thread", lambda **kw: threads.append(real(**kw)) or threads[-1])
    window = _Window()
    boot.start_worker(window)
    threads[0].join(10)
    return window


def test_the_payloads_packages_go_in_before_the_first_checkout(bootstrap, monkeypatch):
    calls = []
    window = _run_worker(bootstrap, monkeypatch, calls)
    steps = [step for step in calls if step[0] != "runtime"]
    assert steps == [("deps", bootstrap.PAYLOAD), ("sync", None), ("deps", bootstrap.APP_DIR)]
    assert window.posts[-1] == "done"


def test_a_reinstall_over_a_clone_has_the_payloads_packages_first_too(bootstrap, monkeypatch):
    (bootstrap.APP_DIR / ".git").mkdir(parents=True)
    (bootstrap.APP_DIR / "desktop").mkdir()
    (bootstrap.APP_DIR / "desktop" / "requirements.txt").write_text("x\n")
    calls = []
    _run_worker(bootstrap, monkeypatch, calls)
    assert [step for step in calls if step[0] != "runtime"] == \
        [("deps", bootstrap.PAYLOAD), ("sync", None), ("deps", bootstrap.APP_DIR)]


def test_the_macos_runtime_is_in_place_before_the_payloads_packages(macos, monkeypatch):
    calls = []
    _run_worker(macos, monkeypatch, calls)
    assert [step[0] for step in calls] == ["runtime", "deps", "sync", "deps"]
