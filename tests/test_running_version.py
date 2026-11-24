"""What a node says it runs (backend/config.py, desktop/updater.head_release).

Until 2026-11-24 a constant said it: "Starting Sautium v0.1.1" on every
release after 0.1.1, in /config and in the API's title as well. Now the
version is the release the checkout is ON — the tag of the version the tree
states, naming HEAD's commit — read from `.git` by hand as HEAD is, and off a
release a node says its commit alone. Real repositories, made with git; the
reader itself spawns nothing.
"""

import subprocess
import sys

import pytest

from tests.conftest import REPO_ROOT

if str(REPO_ROOT / "backend") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "backend"))

from desktop import updater  # noqa: E402

pytestmark = pytest.mark.skipif(not updater.shutil.which("git"), reason="git not installed")


def _git(repo, *args: str) -> str:
    env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.org", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@example.org", "GIT_CONFIG_GLOBAL": "/dev/null",
           "GIT_CONFIG_SYSTEM": "/dev/null", "HOME": str(repo), "PATH": updater.os.environ["PATH"]}
    done = subprocess.run(["git", "-c", "gc.auto=0", *args], cwd=repo, env=env, capture_output=True, text=True)
    assert done.returncode == 0, done.stderr
    return done.stdout.strip()


def _commit(repo, text: str) -> str:
    (repo / "file").write_text(text)
    _git(repo, "add", "file")
    _git(repo, "commit", "-q", "-m", text)
    return _git(repo, "rev-parse", "HEAD")


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """A checkout whose tree states 1.2.3, one commit in."""
    monkeypatch.setattr(updater, "installer_version", lambda: (1, 2, 3))
    repo = tmp_path / "checkout"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _commit(repo, "one")
    return repo


def _release(repo):
    """updater.head_release with every spawn refused: it reads files."""
    real = subprocess.run
    subprocess.run = lambda *a, **kw: (_ for _ in ()).throw(AssertionError("the reader spawned a process"))
    try:
        return updater.head_release(repo)
    finally:
        subprocess.run = real


def test_a_checkout_on_its_release_tag_says_the_release(repo):
    assert _release(repo) is None                                   # no tag yet
    _git(repo, "tag", "-a", "v1.2.3", "-m", "Sautium 1.2.3")
    assert _release(repo) == "v1.2.3"                               # a loose ref, a loose tag object


def test_the_tag_is_found_packed_too(repo):
    _git(repo, "tag", "-a", "v1.2.3", "-m", "Sautium 1.2.3")
    _git(repo, "repack", "-adq")                                    # the tag object in a pack, its ref loose
    assert not list((repo / ".git" / "objects").glob("??/*"))
    assert _release(repo) == "v1.2.3"
    _git(repo, "pack-refs", "--all")                                # ... and the ref packed, peeled
    assert not (repo / ".git" / "refs" / "tags" / "v1.2.3").exists()
    assert _release(repo) == "v1.2.3"


def test_a_release_the_updater_fetched_is_read_from_its_own_namespace(repo):
    """An installed node holds its releases under refs/sautium/releases, with
    no refs/tags of them (desktop/updater._fetch_release, the host agent)."""
    _git(repo, "tag", "-a", "v1.2.3", "-m", "Sautium 1.2.3")
    _git(repo, "update-ref", updater.RELEASE_REFS + "v1.2.3", _git(repo, "rev-parse", "refs/tags/v1.2.3"))
    _git(repo, "tag", "-d", "v1.2.3")
    assert _release(repo) == "v1.2.3"
    _git(repo, "repack", "-adq")
    assert _release(repo) == "v1.2.3"


def test_a_commit_past_the_release_says_no_release(repo):
    _git(repo, "tag", "-a", "v1.2.3", "-m", "Sautium 1.2.3")
    _commit(repo, "two")
    assert _release(repo) is None
    _git(repo, "pack-refs", "--all")
    assert _release(repo) is None


def test_only_the_tag_of_the_version_the_tree_states_is_a_release(repo, monkeypatch):
    _git(repo, "tag", "-a", "v9.9.9", "-m", "another name on HEAD")
    assert _release(repo) is None
    _git(repo, "tag", "v1.2.3")                                     # a lightweight tag is no release
    assert _release(repo) is None
    _git(repo, "tag", "-d", "v1.2.3")
    # an annotated tag object of another name under the release's ref
    _git(repo, "update-ref", "refs/tags/v1.2.3", _git(repo, "rev-parse", "refs/tags/v9.9.9"))
    assert _release(repo) is None
    monkeypatch.setattr(updater, "installer_version", lambda: (0, 0, 0))    # a tree that states none
    assert _release(repo) is None


def test_a_linked_worktree_reads_the_repositorys_tags(repo, tmp_path):
    _git(repo, "tag", "-a", "v1.2.3", "-m", "Sautium 1.2.3")
    linked = tmp_path / "linked"
    _git(repo, "worktree", "add", "-q", "--detach", str(linked), "v1.2.3")
    assert _release(linked) == "v1.2.3"
    assert updater.head_commit(linked) == _git(repo, "rev-parse", "HEAD")[:12]


def test_a_folder_that_is_no_checkout_says_none(tmp_path):
    assert updater.head_release(tmp_path) is None and updater.head_commit(tmp_path) is None


# -- the words -------------------------------------------------------------------

def test_the_start_line_and_the_version_say_the_release_or_the_commit(repo, monkeypatch):
    import config
    monkeypatch.setenv("SAUTIUM_CHECKOUT", str(repo))
    monkeypatch.setattr(updater, "installed_build", lambda: None)
    head = _git(repo, "rev-parse", "HEAD")[:12]
    assert config.build_identity() == {"release": None, "commit": head, "build": None}
    assert config.running_line() == f"Sautium (commit {head})"
    assert config.running_version() == f"commit {head}"
    _git(repo, "tag", "-a", "v1.2.3", "-m", "Sautium 1.2.3")
    assert config.build_identity() == {"release": "v1.2.3", "commit": head, "build": None}
    assert config.running_line() == f"Sautium 1.2.3 ({head})"
    assert config.running_version() == "1.2.3"


def test_a_packaged_tree_says_its_build(tmp_path, monkeypatch):
    import config
    monkeypatch.setenv("SAUTIUM_CHECKOUT", str(tmp_path))
    monkeypatch.setattr(updater, "installed_build", lambda: "1.2.3+0123456789ab")
    assert config.running_line() == "Sautium (build 1.2.3+0123456789ab)"
    assert config.running_version() == "build 1.2.3+0123456789ab"
    monkeypatch.setattr(updater, "installed_build", lambda: None)
    assert config.running_line() == "Sautium (unknown build)"


def test_no_constant_says_the_version():
    import config
    assert not hasattr(config.settings, "app_version")


def test_config_says_the_release_as_its_version(monkeypatch):
    """/config, which a support bundle reads its `app_version` from
    (desktop/diag_bundle): the release without its `v`, and none off one."""
    import asyncio
    monkeypatch.chdir(REPO_ROOT / "backend")        # main mounts static/ relative to its directory
    import main
    identity = {"release": "v1.2.3", "commit": "0123456789ab", "build": None}
    monkeypatch.setattr(main, "build_identity", lambda: dict(identity))
    said = asyncio.run(main.get_config())
    assert {k: said[k] for k in ("app_name", "app_version", "release", "commit", "build")} == {
        "app_name": "Sautium", "app_version": "1.2.3", **identity}
    identity["release"] = None
    assert asyncio.run(main.get_config())["app_version"] is None
