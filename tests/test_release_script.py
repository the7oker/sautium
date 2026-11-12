"""The maintainer's release script (scripts/release.py): it signs a tag only
for a pushed commit of main, a new and higher version the tree itself states,
a key list that parses whole and a history launchers can follow; it verifies
the result with the launcher's own verifier against the list installed
launchers hold, deletes a tag that fails, and never pushes. Real `git tag -s`
through real ssh-keygen with throwaway keys, a bare origin."""

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "release.py"

pytestmark = pytest.mark.skipif(shutil.which("ssh-keygen") is None, reason="no ssh-keygen")


def git(cwd, *args, check=True):
    return subprocess.run(["git", *args], cwd=cwd, check=check,
                          capture_output=True, text=True).stdout.strip()


class Stand:
    def __init__(self, tmp_path):
        self.tmp = tmp_path
        self.keys = {}
        self.origin = tmp_path / "origin.git"
        git(tmp_path, "init", "-q", "--bare", "-b", "main", str(self.origin))
        self.repo = tmp_path / "maint"
        git(tmp_path, "clone", "-q", str(self.origin), str(self.repo))
        git(self.repo, "config", "gpg.format", "ssh")
        self.use_key("daily")
        self.commit("0.1.1", signers=["daily"])

    def key(self, name) -> Path:
        if name not in self.keys:
            path = self.tmp / f"key-{name}"
            subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", name, "-f", str(path)],
                           check=True)
            self.keys[name] = path
        return self.keys[name]

    def line(self, name) -> str:
        kind, b64 = Path(f"{self.key(name)}.pub").read_text().split()[:2]
        return f'{name} namespaces="git" {kind} {b64}'

    def use_key(self, name):
        git(self.repo, "config", "user.signingkey", str(self.key(name)))

    def commit(self, version, signers=None, raw_signers=None, push=True) -> str:
        (self.repo / "desktop").mkdir(exist_ok=True)
        (self.repo / "desktop" / "build_common.py").write_text(f'VERSION = "{version}"\n')
        if signers is not None or raw_signers is not None:
            text = raw_signers if raw_signers is not None else "".join(self.line(n) + "\n" for n in signers)
            (self.repo / "desktop" / "release_signers").write_text(text)
        (self.repo / "app.txt").write_text(f"{version} {len(git(self.repo, 'log', '--oneline', check=False))}\n")
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-qm", version)
        if push:
            git(self.repo, "push", "-q", "-f", "origin", "HEAD:main")
        return git(self.repo, "rev-parse", "HEAD")

    def run(self, *args):
        return subprocess.run([sys.executable, str(SCRIPT), *args, "--repo", str(self.repo)],
                              capture_output=True, text=True)

    def release(self, version, *flags):
        """Tag and push, as the maintainer does."""
        result = self.run("tag", version, *flags)
        assert result.returncode == 0, result.stdout + result.stderr
        git(self.repo, "push", "-q", "origin", f"refs/tags/v{version}")
        return result


@pytest.fixture
def stand(tmp_path, monkeypatch):
    for key, value in {"GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1",
                       "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                       "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}.items():
        monkeypatch.setenv(key, value)
    return Stand(tmp_path)


def remote_tags(stand):
    return git(stand.repo, "ls-remote", "--tags", "origin")


def test_the_floor_is_signed_verified_and_never_pushed(stand):
    tip = stand.commit("0.2.0")
    result = stand.run("tag", "0.2.0")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "is the floor" in result.stdout
    assert f"push origin refs/tags/v0.2.0" in result.stdout
    assert git(stand.repo, "rev-parse", "v0.2.0^{commit}") == tip
    assert git(stand.repo, "cat-file", "-t", "v0.2.0") == "tag"
    assert remote_tags(stand) == ""


def test_a_commit_not_on_origin_main_is_refused(stand):
    stand.commit("0.2.0", push=False)
    result = stand.run("tag", "0.2.0", "--commit", "HEAD")
    assert result.returncode != 0 and "not on origin/main" in result.stderr
    assert git(stand.repo, "tag") == ""


def test_a_version_not_above_the_last_signed_release_is_refused(stand):
    stand.commit("0.3.0")
    stand.release("0.3.0")
    stand.commit("0.2.0")
    result = stand.run("tag", "0.2.0")
    assert result.returncode != 0 and "not above v0.3.0, the last signed release" in result.stderr


def test_an_unsigned_tag_above_blocks_nothing(stand):
    stand.commit("0.2.0")
    git(stand.repo, "tag", "v99.0.0")
    git(stand.repo, "push", "-q", "origin", "refs/tags/v99.0.0")
    git(stand.repo, "tag", "-d", "v99.0.0")
    assert stand.run("tag", "0.2.0").returncode == 0


def test_a_name_origin_already_has_is_refused(stand):
    stand.commit("0.2.0")
    git(stand.repo, "tag", "v0.2.0")
    git(stand.repo, "push", "-q", "origin", "refs/tags/v0.2.0")
    git(stand.repo, "tag", "-d", "v0.2.0")
    result = stand.run("tag", "0.2.0")
    assert result.returncode != 0 and "origin already has a tag v0.2.0" in result.stderr


def test_the_floors_version_is_refused(stand):
    result = stand.run("tag", "0.1.1")
    assert result.returncode != 0 and "not above v0.1.1" in result.stderr


def test_the_default_commit_is_this_checkouts_head_not_origins_tip(stand, tmp_path):
    mine = stand.commit("0.2.0")
    other = tmp_path / "other"
    git(tmp_path, "clone", "-q", str(stand.origin), str(other))
    (other / "app.txt").write_text("a push nobody meant\n")
    git(other, "commit", "-qam", "docs: typo")
    git(other, "push", "-q", "origin", "HEAD:main")
    result = stand.run("tag", "0.2.0")
    assert result.returncode == 0, result.stdout + result.stderr
    assert git(stand.repo, "rev-parse", "v0.2.0^{commit}") == mine


def test_a_head_not_pushed_is_refused(stand):
    stand.commit("0.2.0", push=False)
    result = stand.run("tag", "0.2.0")
    assert result.returncode != 0 and "not on origin/main" in result.stderr


def test_the_commits_the_release_carries_are_shown(stand):
    stand.commit("0.2.0")
    stand.release("0.2.0")
    stand.commit("0.2.9")
    stand.commit("0.3.0")
    result = stand.run("tag", "0.3.0")
    assert "carries 2 commit(s) since v0.2.0" in result.stdout
    assert " 0.2.9" in result.stdout and " 0.3.0" in result.stdout


def test_a_change_to_the_key_list_needs_its_flag(stand):
    stand.commit("0.2.0")
    stand.release("0.2.0")
    stand.commit("0.3.0", signers=["daily", "pusher"])     # one key line more, on a commit of main
    result = stand.run("tag", "0.3.0")
    assert result.returncode != 0 and "--list-change" in result.stderr
    assert "pusher  ssh-ed25519  SHA256:" in result.stdout
    assert git(stand.repo, "tag", "-l", "v0.3.0") == ""
    assert stand.run("tag", "0.3.0", "--list-change").returncode == 0


def test_a_list_with_no_key_for_git_is_refused(stand):
    stand.commit("0.2.0", raw_signers=stand.line("daily").replace('namespaces="git"', 'namespaces="sautium-file"'))
    result = stand.run("tag", "0.2.0", "--list-change")
    assert result.returncode != 0 and "0 for `git`" in result.stderr


def _push_tag_object(stand, name, commit, key=None):
    """A tag pushed by someone else: unsigned annotated, or signed by `key`."""
    args = ["tag", "-a", "-m", "x", name, commit]
    if key:
        args = ["-c", f"user.signingkey={stand.key(key)}", "tag", "-s", "-m", "x", name, commit]
    git(stand.repo, *args)
    git(stand.repo, "push", "-q", "origin", f"refs/tags/{name}")
    git(stand.repo, "tag", "-d", name)


def test_an_unsigned_tag_below_the_floor_breaks_nothing(stand):
    tip = stand.commit("0.2.0")
    _push_tag_object(stand, "v0.1.2", tip)
    result = stand.run("tag", "0.2.0")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "v0.1.2: not in the release chain" in result.stdout


def test_a_pushers_own_signed_tag_cannot_root_the_chain(stand):
    stand.commit("0.1.2", signers=["daily", "pusher"])     # a pushed commit lists the pusher's key too
    _push_tag_object(stand, "v0.1.2", "HEAD", key="pusher")
    stand.commit("0.2.0", signers=["daily"])
    stand.release("0.2.0", "--list-change")
    assert stand.run("verify", "0.2.0").returncode == 0
    impostor = stand.run("verify", "0.1.2")
    assert impostor.returncode != 0 and "not in the release chain" in impostor.stderr


def test_a_version_the_tree_does_not_state_is_refused(stand):
    stand.commit("0.1.1")
    result = stand.run("tag", "0.2.0")
    assert result.returncode != 0 and "VERSION = '0.1.1', not '0.2.0'" in result.stderr


def test_a_key_list_with_a_broken_line_is_refused(stand):
    stand.commit("0.2.0", raw_signers=stand.line("daily") + "\nrecovery cert-authority ssh-ed25519 AAAA\n")
    result = stand.run("tag", "0.2.0")
    assert result.returncode != 0 and "2 key line(s), 1 of them valid" in result.stderr


def test_an_empty_key_list_is_refused(stand):
    stand.commit("0.2.0", raw_signers="")
    result = stand.run("tag", "0.2.0")
    assert result.returncode != 0 and "0 key line(s)" in result.stderr


def test_a_tag_by_a_key_launchers_do_not_hold_is_deleted_again(stand):
    stand.commit("0.2.0")
    stand.use_key("stranger")
    result = stand.run("tag", "0.2.0")
    assert result.returncode != 0 and "deleted again" in result.stderr and "not listed" in result.stderr
    assert git(stand.repo, "tag") == ""


def test_the_next_release_is_verified_against_the_list_the_last_one_carries(stand):
    stand.commit("0.2.0")
    stand.release("0.2.0")
    stand.commit("0.3.0", signers=["recovery"])           # rotates the list, signed by the daily key
    result = stand.release("0.3.0", "--list-change")
    assert "Last signed release: v0.2.0" in result.stdout

    stand.commit("0.4.0")
    refused = stand.run("tag", "0.4.0")                    # the daily key is rotated out
    assert refused.returncode != 0 and "deleted again" in refused.stderr
    stand.use_key("recovery")
    assert stand.run("tag", "0.4.0").returncode == 0


def test_a_release_off_the_last_ones_history_is_refused(stand):
    stand.commit("0.2.0")
    stand.release("0.2.0")
    git(stand.repo, "checkout", "-q", "--orphan", "rewritten")
    stand.commit("0.3.0", signers=["daily"])
    result = stand.run("tag", "0.3.0")
    assert result.returncode != 0 and "does not descend from v0.2.0" in result.stderr


def test_verify_says_whether_launchers_take_a_release(stand):
    tip = stand.commit("0.2.0")
    stand.release("0.2.0")
    assert stand.run("verify", "0.2.0", "--commit", tip).returncode == 0
    wrong = stand.run("verify", "0.2.0", "--commit", "HEAD~1")
    assert wrong.returncode != 0 and "names" in wrong.stderr
    stand.commit("0.3.0")
    git(stand.repo, "tag", "-a", "-m", "unsigned", "v0.3.0")
    git(stand.repo, "push", "-q", "origin", "refs/tags/v0.3.0")
    unsigned = stand.run("verify", "0.3.0")
    assert unsigned.returncode != 0 and "not in the release chain" in unsigned.stderr


def test_a_signed_tag_off_the_chains_history_is_not_in_it(stand):
    stand.commit("0.2.0")
    stand.release("0.2.0")
    git(stand.repo, "checkout", "-q", "--orphan", "elsewhere")
    stand.commit("0.3.0", signers=["daily"], push=False)
    _push_tag_object(stand, "v0.3.0", "HEAD", key="daily")
    result = stand.run("verify", "0.3.0")
    assert result.returncode != 0 and "does not descend from v0.2.0" in result.stdout


def test_git_verify_tag_runs_too_when_the_checkout_names_its_list(stand):
    stand.commit("0.2.0")
    git(stand.repo, "config", "gpg.ssh.allowedSignersFile", str(stand.repo / "desktop" / "release_signers"))
    result = stand.run("tag", "0.2.0")
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'Good "git" signature for daily' in result.stderr


def test_the_selftest_signs_with_the_configured_key_and_verifies_it(stand):
    result = stand.run("selftest")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "OK: signed by daily (ssh-ed25519)" in result.stdout
    assert git(stand.repo, "tag") == ""


def test_the_selftest_refuses_a_key_the_list_does_not_hold(stand):
    stand.use_key("stranger")
    result = stand.run("selftest")
    assert result.returncode != 0 and "not listed" in result.stderr
