"""The maintainer's release script (scripts/release.py): it signs a tag only
for a pushed commit of main, a new and higher version the tree itself states,
a key list that parses whole and a history launchers can follow; it verifies
the result with the launcher's own verifier against the list installed
launchers hold, deletes a tag that fails, and never pushes. Real `git tag -s`
through real ssh-keygen with throwaway keys, a bare origin."""

import os
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

    def line(self, name, namespaces="git") -> str:
        kind, b64 = Path(f"{self.key(name)}.pub").read_text().split()[:2]
        return f'{name} namespaces="{namespaces}" {kind} {b64}'

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

    def run(self, *args, typed=None):
        return subprocess.run([sys.executable, str(SCRIPT), *args, "--repo", str(self.repo)],
                              capture_output=True, text=True, input=typed)

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


def test_a_key_launchers_do_not_hold_is_refused_before_it_signs(stand):
    stand.commit("0.2.0")
    stand.use_key("stranger")
    result = stand.run("tag", "0.2.0")
    assert result.returncode != 0 and "not one of the keys above" in result.stderr
    assert "Signing" not in result.stdout and git(stand.repo, "tag") == ""


def test_the_next_release_is_verified_against_the_list_the_last_one_carries(stand):
    stand.commit("0.2.0", signers=["daily", "recovery"])
    stand.release("0.2.0", "--list-change")
    stand.commit("0.3.0", signers=["recovery", "new"])     # rotates the daily key out
    stand.use_key("recovery")
    result = stand.release("0.3.0", "--list-change")
    assert "Last signed release: v0.2.0" in result.stdout

    stand.commit("0.4.0")
    stand.use_key("daily")
    refused = stand.run("tag", "0.4.0")                    # the daily key is rotated out
    assert refused.returncode != 0 and "not one of the keys above" in refused.stderr
    stand.use_key("new")
    assert stand.run("tag", "0.4.0").returncode == 0


def test_a_release_whose_own_list_drops_its_signer_is_refused_before_it_signs(stand):
    """A rotation signed by the key it drops leaves every installer built
    from it unable to verify anything."""
    stand.commit("0.2.0", signers=["daily", "recovery"])
    stand.release("0.2.0", "--list-change")
    stand.commit("0.3.0", signers=["recovery", "new"])     # drops the daily key, which would sign it
    result = stand.run("tag", "0.3.0", "--list-change")
    assert result.returncode != 0
    assert "does not hold for `git`" in result.stderr and "Sign a rotation with a key the new list keeps" \
        in result.stderr
    assert "SHA256:" in result.stderr and "Signing" not in result.stdout
    assert git(stand.repo, "tag", "-l", "v0.3.0") == ""


def test_the_signing_key_is_read_from_a_private_key_without_its_pub(stand):
    stand.commit("0.2.0")
    Path(f"{stand.key('daily')}.pub").unlink()                # ssh-keygen -y derives it
    assert stand.run("tag", "0.2.0").returncode == 0


# ── the chain is the launchers' ───────────────────────────────────────────

def test_a_stolen_keys_tag_above_a_rotation_is_not_in_the_chain(stand):
    stand.commit("0.2.0", signers=["daily", "recovery"])
    stand.release("0.2.0", "--list-change")
    stand.commit("0.3.0", signers=["recovery", "new"])
    stand.use_key("recovery")
    stand.release("0.3.0", "--list-change")
    stand.commit("0.4.0", push=True)
    _push_tag_object(stand, "v0.4.0", "HEAD", key="daily")
    result = stand.run("verify", "0.4.0")
    assert result.returncode != 0 and "v0.4.0: not in the release chain" in result.stdout


def test_a_fork_blocks_the_next_release_until_its_tag_is_gone(stand):
    floor = stand.commit("0.2.0", signers=["daily", "recovery"])
    stand.release("0.2.0", "--list-change")
    stand.commit("0.3.0")
    stand.release("0.3.0")
    git(stand.repo, "checkout", "-q", "--detach", floor)
    stand.commit("0.2.5", push=False)
    _push_tag_object(stand, "v0.2.5", "HEAD", key="daily")     # a second signed continuation of v0.2.0
    git(stand.repo, "checkout", "-q", "main")
    stand.commit("0.4.0")
    result = stand.run("tag", "0.4.0")
    assert result.returncode != 0 and "the release chain forks at v0.2.0" in result.stderr
    assert stand.run("verify", "0.3.0").returncode != 0            # launchers at v0.2.0 take neither
    git(stand.repo, "push", "-q", "origin", ":refs/tags/v0.2.5")
    assert stand.run("tag", "0.4.0").returncode == 0


# ── a new root: every key lost ────────────────────────────────────────────

def _new_root(stand):
    stand.commit("0.2.0", signers=["daily", "recovery"])
    stand.release("0.2.0", "--list-change")
    stand.commit("0.3.0", signers=["daily2", "recovery2"])
    stand.use_key("daily2")


def test_new_keys_without_new_root_are_refused(stand):
    _new_root(stand)
    result = stand.run("tag", "0.3.0", "--list-change")
    assert result.returncode != 0 and "not one of the keys above" in result.stderr


def test_a_new_root_needs_its_confirmation_typed(stand):
    _new_root(stand)
    floor = git(stand.repo, "config", "--get", "sautium.releaseFloor")
    for typed in (None, "yes\n", "new root v0.3\n", "not new root v0.3.0\n"):
        result = stand.run("tag", "0.3.0", "--new-root", typed=typed)
        assert result.returncode != 0 and "not confirmed — nothing was signed" in result.stderr
        assert "stays on the release it has" in result.stdout and "daily2" in result.stdout
    assert git(stand.repo, "tag") == "v0.2.0"
    assert git(stand.repo, "config", "--get", "sautium.releaseFloor") == floor


def test_a_new_root_becomes_the_floor_and_prints_the_notice(stand):
    _new_root(stand)
    root = stand.commit("0.3.0", signers=["daily2", "recovery2"])
    result = stand.run("tag", "0.3.0", "--new-root", typed="new root v0.3.0\n")
    assert result.returncode == 0, result.stdout + result.stderr
    oid = git(stand.repo, "rev-parse", "refs/tags/v0.3.0")
    assert git(stand.repo, "config", "--get", "sautium.releaseFloor") == oid
    assert "starts a new chain of signed releases" in result.stdout and oid in result.stdout
    git(stand.repo, "push", "-q", "origin", "refs/tags/v0.3.0")
    assert stand.run("verify", "0.3.0", "--commit", root).returncode == 0
    old = stand.run("verify", "0.2.0")
    assert old.returncode != 0 and "not in the release chain" in old.stderr
    stand.commit("0.4.0")
    assert stand.run("tag", "0.4.0").returncode == 0                # the new chain goes on
    stand.use_key("daily")
    assert stand.run("tag", "0.4.0").returncode != 0


def test_release_publish_accepts_a_new_roots_installers(stand, tmp_path):
    _new_root(stand)
    stand.run("tag", "0.3.0", "--new-root", typed="new root v0.3.0\n")
    git(stand.repo, "push", "-q", "origin", "refs/tags/v0.3.0")
    installer = tmp_path / "Sautium-0.3.0-x64.exe"
    installer.write_bytes(b"an installer")
    # The tree the installers were built from carries the script and the
    # launcher's verifier; here they stand beside the stand's tracked files.
    (stand.repo / "scripts").mkdir()
    shutil.copy2(SCRIPT, stand.repo / "scripts" / "release.py")
    for name in ("__init__.py", "updater.py", "release_verify.py"):
        shutil.copy2(SCRIPT.parent.parent / "desktop" / name, stand.repo / "desktop" / name)
    publish = SCRIPT.parent / "release-publish.sh"
    result = subprocess.run(["bash", str(publish), "--tree", str(stand.repo), "--dry-run", str(installer)],
                            capture_output=True, text=True, env={**os.environ, "PYTHON": sys.executable,
                                                                  "DIST_DIR": str(tmp_path / "dist")})
    assert result.returncode == 0, result.stdout + result.stderr
    assert "a real run stops here" not in result.stdout


def test_a_new_root_keeps_no_key_of_the_old_list(stand):
    """Review round 1: a "new root" signed by a key the old list still holds
    is an ordinary update for launchers, whatever the notice says; a rewrite
    with the same keys likewise."""
    stand.commit("0.2.0", signers=["daily", "recovery"])
    stand.release("0.2.0", "--list-change")
    stand.commit("0.3.0", signers=["recovery", "daily2"])
    stand.use_key("recovery")
    result = stand.run("tag", "0.3.0", "--new-root", typed="new root v0.3.0\n")
    assert result.returncode != 0 and "not a new root" in result.stderr and "still on v0.2.0's list" in result.stderr
    git(stand.repo, "checkout", "-q", "--orphan", "rewritten")
    stand.commit("0.3.1", signers=["daily", "recovery"])
    stand.use_key("daily")
    assert "not a new root" in stand.run("tag", "0.3.1", "--new-root", typed="new root v0.3.1\n").stderr


def test_the_fork_refusal_names_the_pair_at_the_cut(stand):
    floor = stand.commit("0.2.0", signers=["daily", "recovery"])
    stand.release("0.2.0", "--list-change")
    c3 = stand.commit("0.3.0")
    stand.release("0.3.0")
    stand.commit("0.4.0")
    stand.release("0.4.0")
    git(stand.repo, "checkout", "-q", "--detach", c3)
    stand.commit("0.4.5", push=False)
    _push_tag_object(stand, "v0.4.5", "HEAD", key="daily")           # beside v0.4.0, on v0.3.0
    git(stand.repo, "checkout", "-q", "--detach", floor)
    stand.commit("0.5.0", push=False)
    _push_tag_object(stand, "v0.5.0", "HEAD", key="daily")           # beside v0.3.0, on v0.2.0
    git(stand.repo, "checkout", "-q", "main")
    stand.commit("0.6.0")
    result = stand.run("tag", "0.6.0")
    assert result.returncode != 0 and "forks at v0.2.0: v0.5.0 and v0.3.0" in result.stderr


def test_a_new_root_is_above_the_old_chain(stand):
    _new_root(stand)
    stand.commit("0.2.0", signers=["daily2", "recovery2"])
    result = stand.run("tag", "0.2.0", "--new-root", typed="new root v0.2.0\n")
    assert result.returncode != 0 and "origin already has a tag v0.2.0" in result.stderr
    stand.commit("0.1.9", signers=["daily2", "recovery2"])
    result = stand.run("tag", "0.1.9", "--new-root", typed="new root v0.1.9\n")
    assert result.returncode != 0 and "not above v0.2.0, the old chain's last release" in result.stderr


# ── the installers' SHA256SUMS ────────────────────────────────────────────

def _sums(stand, tmp_path, signers_text):
    stand.commit("0.2.0", raw_signers=signers_text)
    sums = tmp_path / "SHA256SUMS.txt"
    sums.write_text("ab12  Sautium-0.2.0-x64.exe\n")
    return sums


def test_the_sums_are_signed_for_the_files_namespace_and_verify_with_ssh_keygen(stand, tmp_path):
    lines = stand.line("daily", "git,sautium-file") + "\n" + stand.line("recovery") + "\n"
    sums = _sums(stand, tmp_path, lines)
    result = stand.run("sign-file", str(sums))
    assert result.returncode == 0, result.stdout + result.stderr
    signers = stand.repo / "desktop" / "release_signers"
    verify = ["ssh-keygen", "-Y", "verify", "-f", str(signers), "-I", "daily", "-n", "sautium-file",
              "-s", f"{sums}.sig"]
    good = subprocess.run(verify, stdin=sums.open("rb"), capture_output=True, text=True)
    assert good.returncode == 0 and 'Good "sautium-file" signature for daily' in good.stdout, good.stderr
    as_git = subprocess.run(verify[:7] + ["git"] + verify[8:], stdin=sums.open("rb"), capture_output=True)
    assert as_git.returncode != 0                               # never a release's signature


def test_the_recovery_key_signs_no_sums(stand, tmp_path):
    lines = stand.line("daily", "git,sautium-file") + "\n" + stand.line("recovery") + "\n"
    sums = _sums(stand, tmp_path, lines)
    stand.use_key("recovery")
    result = stand.run("sign-file", str(sums))
    assert result.returncode != 0 and "does not hold for `sautium-file`" in result.stderr
    assert not Path(f"{sums}.sig").exists()


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


def test_the_floor_is_recorded_and_roots_the_chain(stand, tmp_path):
    """Round 2's F: a merge whose first parent is a history of the pusher's
    own, listing the pusher's key, cannot re-root the chain once the floor
    is recorded."""
    stand.commit("0.2.0")
    stand.release("0.2.0")
    floor = git(stand.repo, "config", "--get", "sautium.releaseFloor")
    assert floor == git(stand.repo, "rev-parse", "refs/tags/v0.2.0")
    main = git(stand.repo, "rev-parse", "HEAD")
    git(stand.repo, "checkout", "-q", "--orphan", "theirs")
    stand.commit("0.1.5", signers=["pusher"], push=False)
    _push_tag_object(stand, "v0.1.5", "HEAD", key="pusher")
    theirs = git(stand.repo, "rev-parse", "HEAD")
    tree = git(stand.repo, "rev-parse", f"{main}^{{tree}}")
    merge = git(stand.repo, "commit-tree", tree, "-p", theirs, "-p", main, "-m", "merge")
    git(stand.repo, "push", "-q", "-f", "origin", f"{merge}:refs/heads/main")
    git(stand.repo, "checkout", "-q", "-B", "main", merge)
    assert stand.run("verify", "0.2.0").returncode == 0
    impostor = stand.run("verify", "0.1.5")
    assert impostor.returncode != 0 and "not in the release chain" in impostor.stderr


def test_a_recorded_floor_gone_from_origin_never_makes_a_new_floor(stand):
    stand.commit("0.2.0")
    stand.release("0.2.0")
    git(stand.repo, "push", "-q", "origin", ":refs/tags/v0.2.0")
    stand.commit("0.3.0")
    result = stand.run("tag", "0.3.0")
    assert result.returncode != 0 and "is not on origin" in result.stderr


def test_a_branch_named_like_the_tag_is_no_tag(stand):
    stand.commit("0.2.0")
    git(stand.repo, "push", "-q", "origin", "HEAD:refs/heads/refs/tags/v0.2.0")
    assert stand.run("tag", "0.2.0").returncode == 0


def test_the_derived_root_skips_an_empty_list_and_follows_the_first_parent(stand):
    """Without a recorded floor (an auditor's clone) the root is the first key
    list on origin/main's first-parent line; an empty file before it is none."""
    git(stand.repo, "checkout", "-q", "--orphan", "fresh")
    stand.commit("0.1.1", raw_signers="")
    stand.commit("0.2.0", signers=["daily"])
    stand.release("0.2.0", "--list-change")
    git(stand.repo, "config", "--unset", "sautium.releaseFloor")
    result = stand.run("verify", "0.2.0")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "no floor recorded" in result.stdout


def test_verify_forgets_a_tag_deleted_from_origin(stand):
    stand.commit("0.2.0")
    stand.release("0.2.0")
    stand.commit("0.3.0")
    stand.release("0.3.0")
    assert stand.run("verify", "0.3.0").returncode == 0
    git(stand.repo, "push", "-q", "origin", ":refs/tags/v0.3.0")
    gone = stand.run("verify", "0.3.0")
    assert gone.returncode != 0 and "not in the release chain" in gone.stderr


def test_a_floor_never_pushed_says_how_to_make_it_again(stand):
    stand.commit("0.2.0")
    assert stand.run("tag", "0.2.0").returncode == 0     # made, not pushed
    git(stand.repo, "tag", "-d", "v0.2.0")
    result = stand.run("tag", "0.2.0")
    assert result.returncode != 0 and "git config --unset sautium.releaseFloor" in result.stderr
