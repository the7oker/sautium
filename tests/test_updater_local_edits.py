"""The updater's move to origin/main when the tree carries edits of its own
(desktop/updater.py reset_to_origin). The tree a carrier installed —
`<data dir>/app` — sets them aside as a patch and moves; any other checkout,
the developer's included, refuses and keeps them. Real git repositories in a
temp dir, `origin` a bare one."""

import subprocess

import pytest

from desktop import updater
from desktop.config_manager import get_data_dir


def git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True,
                          capture_output=True, text=True).stdout.strip()


@pytest.fixture
def world(tmp_path, monkeypatch):
    for key, value in {"GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1",
                       "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                       "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "data"))
    data_dir = get_data_dir()

    origin = tmp_path / "origin.git"
    git(tmp_path, "init", "-q", "--bare", "-b", "main", str(origin))
    seed = tmp_path / "seed"
    git(tmp_path, "clone", "-q", str(origin), str(seed))
    (seed / "app.txt").write_text("v1\n")
    git(seed, "add", "app.txt")
    git(seed, "commit", "-qm", "v1")
    git(seed, "push", "-q", "origin", "HEAD:main")

    managed = data_dir / "app"
    working = tmp_path / "work" / "djai"
    for tree in (managed, working):
        git(tmp_path, "clone", "-q", "-b", "main", str(origin), str(tree))

    (seed / "app.txt").write_text("v2\n")
    git(seed, "commit", "-qam", "v2")
    git(seed, "push", "-q", "origin", "HEAD:main")

    def run_in(tree):
        monkeypatch.setattr("desktop.utils.get_project_root", lambda: tree)
        return updater.reset_to_origin()

    return {"managed": managed, "working": working, "tip": git(seed, "rev-parse", "HEAD"),
            "local_edits": data_dir / "local-edits", "run_in": run_in}


def test_managed_tree_sets_its_edits_aside_and_moves(world, tmp_path):
    tree = world["managed"]
    before = git(tree, "rev-parse", "HEAD")
    (tree / "app.txt").write_text("v1 hot-patched\n")

    old_hash, error, set_aside = world["run_in"](tree)

    assert error is None
    assert old_hash == before
    assert git(tree, "rev-parse", "HEAD") == world["tip"]
    assert (tree / "app.txt").read_text() == "v2\n"
    assert set_aside.parent == world["local_edits"]
    # The patch is the edit itself: applied on the commit it was made on, it
    # gives the hot-patched file back.
    restore = tmp_path / "restore"
    git(tree, "worktree", "add", "-q", "--detach", str(restore), before)
    git(restore, "apply", str(set_aside))
    assert (restore / "app.txt").read_text() == "v1 hot-patched\n"


def test_working_tree_keeps_its_edits_and_refuses(world):
    tree = world["working"]
    before = git(tree, "rev-parse", "HEAD")
    (tree / "app.txt").write_text("v1 work in progress\n")

    old_hash, error, set_aside = world["run_in"](tree)

    assert error == "the checkout has local modifications"
    assert set_aside is None
    assert git(tree, "rev-parse", "HEAD") == before
    assert (tree / "app.txt").read_text() == "v1 work in progress\n"
    assert not world["local_edits"].exists()


def test_clean_managed_tree_sets_nothing_aside(world):
    old_hash, error, set_aside = world["run_in"](world["managed"])

    assert (error, set_aside) == (None, None)
    assert git(world["managed"], "rev-parse", "HEAD") == world["tip"]
    assert not world["local_edits"].exists()
