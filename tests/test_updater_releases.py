"""The updater takes signed releases only (desktop/updater.py): origin's `v*`
tags, verified against the INSTALLED tree's desktop/release_signers, the
highest version whose commit descends from HEAD; a rewrite is refused, a
version is never taken backwards, a push to main alone reaches nobody. And the
move itself when the tree carries edits: the tree a carrier installed —
`<data dir>/app` — sets them aside as a patch, any other checkout refuses.
Real git repositories in a temp dir, `origin` a bare one, throwaway keys."""

import os
import subprocess
import sys
from pathlib import Path

import pytest

from desktop import release_verify, updater
from desktop.config_manager import get_data_dir
from desktop.utils import get_project_root
from tests.release_signing import Key, push_signed_tag


def git(cwd, *args, check=True):
    return subprocess.run(["git", *args], cwd=cwd, check=check,
                          capture_output=True, text=True).stdout.strip()


class World:
    def __init__(self, tmp_path, monkeypatch, key):
        self.tmp, self.monkeypatch, self.key = tmp_path, monkeypatch, key
        self.origin = tmp_path / "origin.git"
        git(tmp_path, "init", "-q", "--bare", "-b", "main", str(self.origin))
        git(self.origin, "config", "uploadpack.allowFilter", "true")    # as GitHub does
        self.seed = tmp_path / "seed"
        git(tmp_path, "clone", "-q", str(self.origin), str(self.seed))
        self.base = self.commit("v1\n", signers=[key])
        self.managed = get_data_dir() / "app"
        self.working = tmp_path / "work" / "djai"
        for tree in (self.managed, self.working):
            git(tmp_path, "clone", "-q", "-b", "main", str(self.origin), str(tree))

    def commit(self, app: str, signers=None, version: str = None) -> str:
        (self.seed / "app.txt").write_text(app)
        paths = ["app.txt"]
        if version:
            (self.seed / "desktop").mkdir(exist_ok=True)
            (self.seed / "desktop" / "build_common.py").write_text(f'VERSION = "{version}"\n')
            paths.append("desktop/build_common.py")
        if signers is not None:
            (self.seed / "desktop").mkdir(exist_ok=True)
            (self.seed / "desktop" / "release_signers").write_text(
                "".join(k.line(f"key{i}") + "\n" for i, k in enumerate(signers)))
            paths.append("desktop/release_signers")
        git(self.seed, "add", *paths)
        git(self.seed, "commit", "-qm", app.strip())
        git(self.seed, "push", "-q", "-f", "origin", "HEAD:main")
        return git(self.seed, "rev-parse", "HEAD")

    def tag(self, name: str, commit: str, key: Key = None, **kw) -> None:
        push_signed_tag(self.seed, key or self.key, commit, name, **kw)
        git(self.seed, "push", "-q", "-f", "origin", f"refs/tags/{name}")

    def lightweight(self, name: str, commit: str) -> None:
        git(self.seed, "tag", "-f", name, commit)
        git(self.seed, "push", "-q", "-f", "origin", f"refs/tags/{name}")

    def use(self, tree):
        self.monkeypatch.setattr("desktop.utils.get_project_root", lambda: tree)

    def check(self, tree=None) -> updater.Survey:
        self.use(tree or self.managed)
        return updater.check_for_updates()

    def update(self, tree=None):
        self.use(tree or self.managed)
        return updater.reset_to_release()

    def head(self, tree=None) -> str:
        return git(tree or self.managed, "rev-parse", "HEAD")


@pytest.fixture
def world(tmp_path, monkeypatch):
    for key, value in {"GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1",
                       "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                       "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "data"))
    return World(tmp_path, monkeypatch, Key(sk=True))


# ── what is offered ───────────────────────────────────────────────────────

def test_a_push_to_main_alone_reaches_nobody(world):
    world.commit("v2\n")
    survey = world.check()
    assert (survey.installed, survey.offered, survey.rewrite, survey.refused) == (None, None, None, [])
    old, release, error, _ = world.update()
    assert (release, error) == (None, "no signed release descends from this checkout")
    assert world.head() == world.base


def test_a_normal_release_is_offered_and_taken(world):
    tip = world.commit("v2\n", version="0.2.0")
    world.tag("v0.2.0", tip)
    survey = world.check()
    assert survey.offered == updater.Release("v0.2.0", (0, 2, 0), tip)
    assert survey.offered.label == "Sautium 0.2.0"
    old, release, error, set_aside = world.update()
    assert (old, release.name, error, set_aside) == (world.base, "v0.2.0", None, None)
    assert world.head() == tip
    assert (world.managed / "app.txt").read_text() == "v2\n"
    after = world.check()
    assert (after.installed.name, after.offered) == ("v0.2.0", None)


def test_the_move_goes_to_the_verified_commit_not_to_main(world):
    tagged = world.commit("v2\n")
    world.tag("v0.2.0", tagged)
    world.commit("v3, unreleased\n")
    world.update()
    assert world.head() == tagged


def test_two_releases_at_once_offer_the_highest(world):
    first = world.commit("v2\n")
    world.tag("v0.2.0", first)
    second = world.commit("v3\n")
    world.tag("v0.3.0", second)
    assert world.check().offered.name == "v0.3.0"
    world.update()
    assert world.head() == second


def test_versions_compare_as_numbers(world):
    nine = world.commit("v2\n")
    world.tag("v0.9.0", nine)
    ten = world.commit("v3\n")
    world.tag("v0.10.0", ten)
    assert world.check().offered.name == "v0.10.0"


def test_a_version_is_never_taken_backwards(world):
    tip = world.commit("v2\n", version="0.3.0")
    world.tag("v0.3.0", tip)
    world.update()
    later = world.commit("v3\n")
    world.tag("v0.2.5", later)            # descends from HEAD, a lower version
    survey = world.check()
    assert (survey.installed.name, survey.offered) == ("v0.3.0", None)


def test_a_checkout_ahead_of_the_newest_release_has_it_installed(world):
    tagged = world.commit("v2\n", version="0.2.0")
    world.tag("v0.2.0", tagged)
    world.commit("v3\n")
    tree = world.working
    git(tree, "pull", "-q", "--ff-only", "origin", "main")
    survey = world.check(tree)
    assert (survey.installed.name, survey.offered, survey.rewrite) == ("v0.2.0", None, None)


def test_the_unsigned_tags_before_the_floor_are_never_releases(world):
    world.lightweight("v0.1.0", world.base)
    world.lightweight("v0.1.1", world.base)
    survey = world.check()
    assert (survey.installed, survey.offered, survey.refused) == (None, None, [])


# ── rotation ──────────────────────────────────────────────────────────────

def test_a_release_that_rotates_the_list_governs_the_next(world):
    new_key = Key()
    rotating = world.commit("v2\n", signers=[new_key])
    world.tag("v0.2.0", rotating)                       # signed by the old key
    nxt = world.commit("v3\n")
    world.tag("v0.3.0", nxt, key=new_key)               # by the new key only

    survey = world.check()
    assert survey.offered.name == "v0.2.0"              # v0.3.0 is not yet trusted here
    assert survey.refused == []                         # but it is the chain's next step, no refusal

    world.update()
    assert world.check().offered.name == "v0.3.0"
    world.update()
    assert world.head() == nxt


def test_a_rotated_out_keys_later_tag_is_refused(world):
    new_key = Key()
    rotating = world.commit("v2\n", signers=[new_key])
    world.tag("v0.2.0", rotating)
    world.update()
    later = world.commit("v3\n")
    world.tag("v0.3.0", later)                           # the old key, no longer listed
    survey = world.check()
    assert survey.offered is None
    assert survey.refused[0][0] == "v0.3.0" and "not listed" in survey.refused[0][1]


def test_the_list_is_read_from_head_not_the_working_copy(world):
    stranger = Key()
    tip = world.commit("v2\n")
    world.tag("v0.2.0", tip, key=stranger)
    (world.managed / "desktop" / "release_signers").write_text(stranger.line() + "\n")
    survey = world.check()
    assert survey.offered is None and survey.refused[0][0] == "v0.2.0"


def test_the_list_is_read_from_head_not_the_fetched_tree(world):
    stranger = Key()
    tip = world.commit("v2\n", signers=[stranger])       # the release adds its own key
    world.tag("v0.2.0", tip, key=stranger)
    assert world.check().offered is None


def test_an_empty_installed_list_takes_nothing(world):
    tip = world.commit("v2\n", signers=[])
    world.tag("v0.2.0", tip)
    world.update()                                       # signed by the old list
    later = world.commit("v3\n")
    world.tag("v0.3.0", later)
    assert world.check().offered is None


# ── the chain: a rotation revokes for every launcher ──────────────────────

def off_main(world, parent: str, app: str, signers=None) -> str:
    """A commit on `parent` that main never carries — pushed as a tag only;
    `signers` replaces the list its tree carries."""
    tree = git(world.seed, "rev-parse", f"{parent}^{{tree}}")
    index = world.tmp / "side-index"
    env = {**os.environ, "GIT_INDEX_FILE": str(index)}
    subprocess.run(["git", "read-tree", tree], cwd=world.seed, env=env, check=True)
    files = {"app.txt": app}
    if signers is not None:
        files["desktop/release_signers"] = "".join(k.line(f"key{i}") + "\n" for i, k in enumerate(signers))
    for path, text in files.items():
        blob = subprocess.run(["git", "hash-object", "-w", "--stdin"], cwd=world.seed, input=text, check=True,
                              capture_output=True, text=True).stdout.strip()
        subprocess.run(["git", "update-index", "--add", "--cacheinfo", f"100644,{blob},{path}"], cwd=world.seed,
                       env=env, check=True)
    side = subprocess.run(["git", "write-tree"], cwd=world.seed, env=env, check=True, capture_output=True,
                          text=True).stdout.strip()
    index.unlink()
    return git(world.seed, "commit-tree", side, "-p", parent, "-m", app.strip())


@pytest.fixture
def rotated(world):
    """r0 = v0.2.0 lists {A, R} and is installed; r1 = v0.3.0 rotates the
    list to {B, R}, signed by R: A is the stolen key."""
    a, r, b = world.key, Key(), Key()
    r0 = world.commit("r0\n", signers=[a, r], version="0.2.0")
    world.tag("v0.2.0", r0)
    world.update()
    r1 = world.commit("r1\n", signers=[b, r], version="0.3.0")
    world.tag("v0.3.0", r1, key=r)
    return world, a, r, b, r0, r1


def test_a_stolen_keys_tag_on_the_installed_commit_hides_no_rotation(rotated):
    """Review round 1's H1: the thief signs v0.9.0 with A on r0 itself. HEAD
    contains it and the installed list verifies it, but r0's tree states
    0.2.0, so it is no installed release: the walk still sees the rotation."""
    world, a, r, b, r0, r1 = rotated
    world.tag("v0.9.0", r0, key=a)
    survey = world.check()
    assert (survey.installed.name, survey.held, survey.offered.name) == ("v0.2.0", "v0.9.0", "v0.3.0")
    evil = off_main(world, r0, "thief\n")
    world.tag("v0.9.1", evil, key=a)
    survey = world.check()
    assert survey.offered is None and survey.fork == ("v0.9.1", "v0.3.0")
    world.update()
    assert world.head() == r0


def test_a_stolen_keys_tag_on_a_commit_that_once_stated_a_higher_version_hides_no_rotation(world):
    """Review round 2: a merged commit below r0 said VERSION = "9.0.0"; the
    thief's v9.0.0 there states its own version, but no release HEAD holds
    is above what HEAD itself states."""
    a, r, b = world.key, Key(), Key()
    planted = world.commit("a merged bump\n", signers=[a, r], version="9.0.0")
    r0 = world.commit("r0\n", version="0.2.0")
    world.tag("v0.2.0", r0)
    world.update()
    world.tag("v0.3.0", world.commit("r1\n", signers=[b, r], version="0.3.0"), key=r)
    world.tag("v9.0.0", planted, key=a)
    world.tag("v9.0.1", off_main(world, r0, "thief\n"), key=a)
    survey = world.check()
    assert (survey.installed.name, survey.held, survey.offered) == ("v0.2.0", "v9.0.0", None)
    assert survey.fork == ("v9.0.1", "v0.3.0")
    world.update()
    assert world.head() == r0


def test_a_lower_name_on_a_commit_stating_a_higher_version_is_no_installed_release(world):
    tip = world.commit("v2\n", version="0.3.0")
    world.tag("v0.2.0", tip)
    world.update()
    survey = world.check()
    assert (survey.installed, survey.held) == (None, "v0.2.0")


def test_a_working_tree_ahead_with_its_version_bumped_keeps_its_installed_release(world):
    tagged = world.commit("v2\n", version="0.2.0")
    world.tag("v0.2.0", tagged)
    world.commit("bump\n", version="0.3.0")                # not released yet
    git(world.working, "pull", "-q", "--ff-only", "origin", "main")
    assert world.check(world.working).installed.name == "v0.2.0"


def test_a_fork_below_a_rewrite_is_not_said(rotated):
    world, a, r, b, r0, r1 = rotated
    world.tag("v0.4.0", off_main(world, r0, "thief\n"), key=a)
    git(world.seed, "checkout", "-q", "--orphan", "rewritten")
    world.tag("v0.5.0", world.commit("rewritten\n", signers=[a, r], version="0.5.0"), key=r)
    survey = world.check()
    assert (survey.offered, survey.rewrite.name, survey.fork) == (None, "v0.5.0", None)
    assert not any(name == "v0.4.0" for name, _ in survey.refused)


def test_nested_forks_cut_at_the_lower_link(rotated):
    """The thief's v0.2.5 on r0 (A; its tree lists A and T), v0.2.6 on it (T),
    v0.2.7 beside v0.2.6 (A): forks at v0.2.5 and, with the owner's v0.3.0, at
    r0 — the walk stops at r0, never offering v0.2.5."""
    world, a, r, b, r0, r1 = rotated
    t = Key()
    base = off_main(world, r0, "thief base\n", signers=[a, t])
    world.tag("v0.2.5", base, key=a)
    world.tag("v0.2.6", off_main(world, base, "thief 6\n"), key=t)
    world.tag("v0.2.7", off_main(world, base, "thief 7\n"), key=a)
    survey = world.check()
    assert survey.offered is None and survey.fork == ("v0.3.0", "v0.2.5")


def test_a_lightweight_tag_above_a_fork_hides_no_fork_sentence(rotated):
    world, a, r, b, r0, r1 = rotated
    world.tag("v0.4.0", off_main(world, r0, "thief\n"), key=a)
    world.lightweight("v9.9.9", r1)
    survey = world.check()
    assert survey.refused[0][0] == "v9.9.9" and survey.fork == ("v0.4.0", "v0.3.0")
    assert "Releases v0.4.0 and v0.3.0" in updater.survey_words(survey)[1][-1]


def test_the_fork_sentence_is_never_beside_an_offer():
    offered = updater.Release("v0.2.0", (0, 2, 0), "0" * 40)
    heading, lines = updater.survey_words(updater.Survey(offered=offered, refused=[("v0.4.0", "x")],
                                                         fork=("v0.4.0", "v0.3.0")))
    assert not any("v0.4.0" in line for line in lines)


# ── launchers and scripts/release.py judge one origin alike ──────────────

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "release.py"


def _maintainer(world, floor: str):
    """The owner's checkout of the same origin, its floor recorded as `tag`
    records it."""
    repo = world.tmp / "maint"
    if not repo.exists():
        git(world.tmp, "clone", "-q", str(world.origin), str(repo))
    git(repo, "fetch", "-q", "origin", f"refs/tags/{floor}:refs/tags/{floor}")
    git(repo, "config", "sautium.releaseFloor", git(repo, "rev-parse", f"refs/tags/{floor}"))
    return lambda *args: subprocess.run([sys.executable, str(SCRIPT), *args, "--repo", str(repo)],
                                        capture_output=True, text=True)


def test_release_py_refuses_what_the_launcher_refuses_on_the_installed_commit(rotated):
    world, a, r, b, r0, r1 = rotated
    world.tag("v0.9.0", r0, key=a)
    world.tag("v0.9.1", off_main(world, r0, "thief\n"), key=a)
    script = _maintainer(world, "v0.2.0")
    assert world.check().offered is None
    assert script("verify", "0.9.1").returncode != 0 and script("verify", "0.9.0").returncode != 0


def test_release_py_refuses_a_link_below_a_fork(rotated):
    """Below the cut a thief's lower version is a link of the walk, never a
    release of the chain."""
    world, a, r, b, r0, r1 = rotated
    world.tag("v0.2.5", off_main(world, r0, "thief\n"), key=a)
    script = _maintainer(world, "v0.2.0")
    assert script("verify", "0.2.5").returncode != 0
    assert script("verify", "0.3.0").returncode != 0
    assert world.check().offered is None


def test_a_tag_object_too_large_is_refused_by_both(world):
    """Review round 1's M1: the launcher never reads a tag object over 64 KiB;
    release.py must not take it into the chain either."""
    world.tag("v0.2.0", world.commit("r1\n", version="0.2.0"))
    r2 = world.commit("r2\n", version="0.3.0")
    payload = (f"object {r2}\ntype commit\ntag v0.3.0\ntagger T <t@t> 1794470000 +0000\n\n"
               + "x" * 70000 + "\n").encode()
    oid = subprocess.run(["git", "hash-object", "-t", "tag", "-w", "--stdin"], cwd=world.seed,
                         input=payload + world.key.sign(payload), capture_output=True, check=True).stdout.decode().strip()
    git(world.seed, "update-ref", "refs/tags/v0.3.0", oid)
    git(world.seed, "push", "-q", "-f", "origin", "refs/tags/v0.3.0")
    assert world.check().refused[0] == ("v0.3.0", "a tag too large to be a release tag")
    verdict = _maintainer(world, "v0.2.0")("verify", "0.3.0")
    assert verdict.returncode != 0 and "too large" in verdict.stdout


def test_a_stolen_keys_tag_on_top_of_a_rotation_is_refused_below_it(rotated):
    world, a, r, b, r0, r1 = rotated
    world.tag("v0.4.0", world.commit("thief\n"), key=a)      # on r1; A is in the installed list
    survey = world.check()
    assert survey.offered == updater.Release("v0.3.0", (0, 3, 0), r1)
    assert survey.refused[0][0] == "v0.4.0" and "not listed" in survey.refused[0][1]
    world.update()
    assert world.head() == r1
    assert world.check().offered is None


def test_a_stolen_keys_tag_beside_a_rotation_is_a_fork_and_nothing_above_r0_is_taken(rotated):
    world, a, r, b, r0, r1 = rotated
    world.tag("v0.4.0", off_main(world, r0, "thief\n"), key=a)
    survey = world.check()
    assert survey.offered is None
    assert survey.refused[0][0] == "v0.4.0" and "does not descend from v0.3.0" in survey.refused[0][1]
    assert survey.fork == ("v0.4.0", "v0.3.0")
    assert "Releases v0.4.0 and v0.3.0 continue the same release" in updater.survey_words(survey)[1][-1]
    old, release, error, _ = world.update()
    assert release is None and world.head() == r0
    git(world.seed, "push", "-q", "origin", ":refs/tags/v0.4.0")      # the owner deletes it
    assert world.check().offered.name == "v0.3.0"


def test_a_stolen_keys_lower_version_beside_a_rotation_takes_nothing_either(rotated):
    """The walk goes up by version, and a thief picks any version: one
    below the rotation, on r0, would come first and make the owner's r1 the
    fork. Two signed continuations of one release take neither, whatever
    their versions."""
    world, a, r, b, r0, r1 = rotated
    world.tag("v0.2.5", off_main(world, r0, "thief\n"), key=a)
    survey = world.check()
    assert survey.offered is None
    assert survey.fork == ("v0.3.0", "v0.2.5")


def test_a_launcher_below_a_rotation_takes_it_then_the_new_keys_release(rotated):
    world, a, r, b, r0, r1 = rotated
    r2 = world.commit("r2\n")
    world.tag("v0.4.0", r2, key=b)
    survey = world.check()
    assert (survey.offered.name, survey.refused) == ("v0.3.0", [])
    world.update()
    assert world.check().offered.name == "v0.4.0"
    world.update()
    assert world.head() == r2


def test_two_rotations_are_walked_in_one_check(rotated):
    world, a, r, b, r0, r1 = rotated
    c = Key()
    r2 = world.commit("r2\n", signers=[c, r])
    world.tag("v0.4.0", r2, key=r)
    r3 = world.commit("r3\n")
    world.tag("v0.5.0", r3, key=c)
    world.tag("v0.6.0", world.commit("thief a\n"), key=a)      # rotated out at r1
    world.tag("v0.7.0", world.commit("thief b\n"), key=b)      # rotated out at r2
    survey = world.check()
    assert survey.offered.name == "v0.4.0"                        # both rotations at once: R signed them
    assert [name for name, _ in survey.refused] == ["v0.7.0", "v0.6.0"]
    world.update()
    assert world.check().offered.name == "v0.5.0"
    world.update()
    assert (world.head(), world.check().offered) == (r3, None)


def test_a_key_the_installed_list_holds_is_refused_where_its_predecessor_dropped_it(rotated):
    world, a, r, b, r0, r1 = rotated
    world.tag("v0.4.0", world.commit("r2\n"), key=b)            # B: not in the installed list
    world.tag("v0.5.0", world.commit("t\n"), key=a)             # A: in it, not in r2's
    survey = world.check()
    assert survey.offered.name == "v0.3.0"
    assert survey.refused == [("v0.5.0", survey.refused[0][1])] and "not listed" in survey.refused[0][1]


def test_a_first_install_walks_the_chain_from_the_installers_release(world, monkeypatch):
    a, r, b = world.key, Key(), Key()
    r0 = world.commit("r0\n", signers=[a, r], version="0.2.0")
    world.tag("v0.2.0", r0)
    r1 = world.commit("r1\n", signers=[b, r], version="0.3.0")
    world.tag("v0.3.0", r1, key=r)
    world.tag("v0.9.0", off_main(world, r1, "thief\n"), key=a)  # the payload's list holds A
    monkeypatch.setattr(updater, "installer_version", lambda: (0, 2, 0))
    app, _ = _fresh(world, "app")
    assert updater.install_release(str(app), _payload_list(world, a, r)).name == "v0.3.0"
    assert git(app, "rev-parse", "HEAD") == r1
    r2 = world.commit("r2\n", version="0.4.0")
    world.tag("v0.4.0", r2, key=b)
    assert world.check(app).offered.name == "v0.4.0"


def test_a_first_install_never_starts_below_the_installers_release(world, monkeypatch):
    a, r = world.key, Key()
    world.tag("v0.2.0", world.commit("r0\n", signers=[a, r], version="0.2.0"))
    monkeypatch.setattr(updater, "installer_version", lambda: (0, 3, 0))
    app, _ = _fresh(world, "app")
    assert updater.install_release(str(app), _payload_list(world, a, r)) is None


def test_a_new_root_is_no_update_for_the_old_chain_and_its_installer_moves_on(world, monkeypatch):
    """Both keys lost: a release signed by new keys starts a new chain. An
    installed launcher stays where it is; an installer built at the new root
    — a first install or one over the old clone — follows the new chain."""
    a, r = world.key, Key()
    r0 = world.commit("r0\n", signers=[a, r], version="0.2.0")
    world.tag("v0.2.0", r0)
    world.update()
    n, r2 = Key(sk=True), Key()
    world.tag("v0.3.0", world.commit("new root\n", signers=[n, r2], version="0.3.0"), key=n)
    n1 = world.commit("n1\n", version="0.4.0")
    world.tag("v0.4.0", n1, key=n)
    survey = world.check()
    assert (survey.installed.name, survey.offered, survey.rewrite) == ("v0.2.0", None, None)
    assert [name for name, _ in survey.refused] == ["v0.4.0", "v0.3.0"]
    monkeypatch.setattr(updater, "installer_version", lambda: (0, 3, 0))
    app, _ = _fresh(world, "app")
    assert updater.install_release(str(app), _payload_list(world, n, r2)).name == "v0.4.0"
    assert updater.install_release(str(world.managed), _payload_list(world, n, r2)).name == "v0.4.0"
    assert world.head() == n1
    world.tag("v0.5.0", world.commit("n2\n", version="0.5.0"), key=n)
    assert world.check().offered.name == "v0.5.0"


# ── what is refused ───────────────────────────────────────────────────────

def test_a_signed_release_that_rewrites_history_is_refused(world):
    tip = world.commit("v2\n", version="0.2.0")
    world.tag("v0.2.0", tip)
    world.update()
    git(world.seed, "reset", "-q", "--hard", world.base)
    rewritten = world.commit("v2, rewritten\n")
    world.tag("v0.3.0", rewritten)
    survey = world.check()
    assert (survey.installed.name, survey.offered, survey.rewrite.name) == ("v0.2.0", None, "v0.3.0")
    old, release, error, _ = world.update()
    assert release is None and error
    assert world.head() == tip


def orphan(world):
    """A history that shares nothing with HEAD's, as a rewrite leaves it."""
    git(world.seed, "checkout", "-q", "--orphan", "other")
    (world.seed / "app.txt").write_text("elsewhere\n")
    git(world.seed, "commit", "-qam", "elsewhere")


def test_a_rewrite_below_a_release_that_descends_is_not_reported(world):
    orphan(world)
    world.tag("v0.2.0", git(world.seed, "rev-parse", "HEAD"))
    git(world.seed, "checkout", "-q", "main")
    good = world.commit("v2\n")
    world.tag("v0.3.0", good)
    survey = world.check()
    assert (survey.offered.name, survey.rewrite) == ("v0.3.0", None)


def test_a_rewrite_above_a_release_that_descends_is_said_beside_it(world):
    good = world.commit("v2\n")
    world.tag("v0.2.0", good)
    orphan(world)
    world.tag("v0.3.0", git(world.seed, "rev-parse", "HEAD"))
    git(world.seed, "checkout", "-q", "main")
    survey = world.check()
    assert (survey.offered.name, survey.rewrite.name) == ("v0.2.0", "v0.3.0")


def test_a_lightweight_tag_is_refused(world):
    tip = world.commit("v2\n")
    world.lightweight("v0.2.0", tip)
    survey = world.check()
    assert survey.offered is None
    assert survey.refused == [("v0.2.0", "a lightweight tag, not a signed release")]


def test_a_renamed_tag_is_refused(world):
    tip = world.commit("v2\n")
    world.tag("v9.9.9", tip, header_name="v0.2.0")       # the signed v0.2.0 pushed as v9.9.9
    survey = world.check()
    assert survey.offered is None
    assert survey.refused[0][0] == "v9.9.9" and "named 'v0.2.0'" in survey.refused[0][1]


def test_a_moved_tag_is_judged_as_it_is_now(world):
    tip = world.commit("v2\n")
    world.tag("v0.2.0", tip)
    assert world.check().offered.commit == tip
    elsewhere = world.commit("v3\n")
    world.tag("v0.2.0", elsewhere, key=Key())            # moved, re-signed by a stranger
    survey = world.check()
    assert survey.offered is None and survey.refused[0][0] == "v0.2.0"
    world.lightweight("v0.2.0", elsewhere)               # moved again, unsigned
    assert world.check().refused == [("v0.2.0", "a lightweight tag, not a signed release")]


def test_a_tag_moved_backwards_is_judged_as_it_is_now(world):
    first = world.commit("v2\n")
    later = world.commit("v3\n")
    world.tag("v0.3.0", later)
    assert world.check().offered.commit == later
    world.tag("v0.3.0", first)                           # re-signed, on an older commit: no fast-forward
    assert world.check().offered.commit == first


def test_names_that_are_no_release_never_reach_a_fetch(world):
    tip = world.commit("v2\n")
    world.tag("v0.2.0", tip)
    for junk in ("vИ", "v1.2", "v01.2.3", "v1.2.3-rc1"):
        world.lightweight(junk, tip)
    raw = subprocess.run(["git", "tag", b"v\xff".decode("latin-1")], cwd=world.seed)    # not UTF-8 on the wire
    assert raw.returncode == 0
    subprocess.run(["git", "push", "-q", "origin", "--tags"], cwd=world.seed, check=True)
    survey = world.check()
    assert (survey.offered.name, survey.refused) == ("v0.2.0", [])
    assert git(world.managed, "for-each-ref", "--format=%(refname)", updater.RELEASE_REFS) == \
        f"{updater.RELEASE_REFS}v0.2.0"


def _has(repo, oid) -> bool:
    return subprocess.run(["git", "cat-file", "-e", oid], cwd=repo).returncode == 0


def test_a_tag_that_does_not_verify_costs_no_more_than_its_tag_object(world):
    git(world.seed, "checkout", "-q", "--orphan", "junk")
    (world.seed / "big.bin").write_bytes(os.urandom(2_000_000))
    git(world.seed, "add", "big.bin")
    git(world.seed, "commit", "-qm", "junk")
    junk = git(world.seed, "rev-parse", "HEAD")
    blob = git(world.seed, "rev-parse", "HEAD:big.bin")
    world.tag("v9.0.0", junk, key=Key())                 # a stranger's signature over a big history
    git(world.seed, "checkout", "-q", "main")
    survey = world.check()
    assert survey.refused[0][0] == "v9.0.0"
    assert not _has(world.managed, blob) and not _has(world.managed, junk)
    # The probe is a partial clone: asking it for the blob would fetch it, so
    # its size answers instead.
    probe = Path(git(world.managed, "rev-parse", "--absolute-git-dir")) / updater.PROBE_DIR
    assert sum(f.stat().st_size for f in (probe / "objects").rglob("*") if f.is_file()) < 50_000


def test_a_tag_over_a_long_history_costs_only_its_tip(world):
    lines = []
    for i in range(1, 2001):
        lines += ["commit refs/heads/long", f"committer t <t@t> {1700000000 + i} +0000",
                  f"data {len(str(i))}", str(i), ""]        # each continues the branch it is on
    subprocess.run(["git", "fast-import", "--quiet"], cwd=world.seed, check=True,
                   input="\n".join(lines).encode())
    world.tag("v9.0.0", git(world.seed, "rev-parse", "refs/heads/long"), key=Key())
    assert world.check().refused[0][0] == "v9.0.0"
    probe = Path(git(world.managed, "rev-parse", "--absolute-git-dir")) / updater.PROBE_DIR
    assert sum(f.stat().st_size for f in (probe / "objects").rglob("*") if f.is_file()) < 20_000


def test_a_probe_that_is_no_repository_is_made_again_never_falls_through(world):
    git_dir = Path(git(world.managed, "rev-parse", "--absolute-git-dir"))
    probe = git_dir / updater.PROBE_DIR
    probe.mkdir()
    (probe / "HEAD").write_text("ref: refs/heads/main\n")        # an init that died after HEAD
    tip = world.commit("v2\n")
    world.tag("v0.2.0", tip)
    assert world.check().offered.name == "v0.2.0"
    assert not (git_dir / "shallow").exists()
    assert git(world.managed, "config", "--get", "extensions.partialClone", check=False) == ""


def test_a_git_that_hangs_is_no_survey(world, monkeypatch):
    real = subprocess.run
    def hanging(cmd, **kw):
        if "ls-remote" in cmd:
            raise subprocess.TimeoutExpired(cmd, kw.get("timeout"))
        return real(cmd, **kw)
    monkeypatch.setattr(updater.subprocess, "run", hanging)
    assert world.check() is None


def test_an_origin_at_a_former_address_is_moved_before_the_check(world, monkeypatch, tmp_path):
    tip = world.commit("v2\n")
    world.tag("v0.2.0", tip)
    former = tmp_path / "former-address.git"
    git(world.managed, "remote", "set-url", "origin", str(former))
    monkeypatch.setattr(updater, "ORIGIN_MOVES", {"former-address": str(world.origin)})
    assert world.check().offered.name == "v0.2.0"
    assert git(world.managed, "remote", "get-url", "origin") == str(world.origin)


def test_a_release_that_verifies_is_fetched_with_its_history(world):
    tip = world.commit("v2\n")
    world.tag("v0.2.0", tip)
    world.check()
    assert _has(world.managed, f"{tip}^{{tree}}")


def test_a_deleted_tag_leaves_with_its_release(world):
    tip = world.commit("v2\n")
    world.tag("v0.2.0", tip)
    assert world.check().offered
    git(world.seed, "push", "-q", "origin", ":refs/tags/v0.2.0")
    assert world.check().offered is None
    assert git(world.managed, "for-each-ref", updater.RELEASE_REFS) == ""


def test_a_tag_moved_upstream_never_fails_the_fetch(world):
    tip = world.commit("v2\n")
    world.lightweight("v0.2.0", tip)
    git(world.managed, "fetch", "-q", "--tags", "origin")   # refs/tags/v0.2.0 here, as a person's fetch leaves it
    world.tag("v0.2.0", tip)                                # replaced upstream by the signed one
    assert world.check().offered.name == "v0.2.0"


def test_refused_tags_below_the_installed_release_are_not_judged(world):
    world.lightweight("v0.1.5", world.base)
    tip = world.commit("v2\n", version="0.2.0")
    world.tag("v0.2.0", tip)
    world.update()
    assert world.check().refused == []


def test_no_signed_tag_at_all_offers_nothing(world):
    tip = world.commit("v2\n")
    world.lightweight("v0.2.0", tip)
    world.lightweight("latest", tip)
    survey = world.check()
    assert (survey.installed, survey.offered, survey.rewrite) == (None, None, None)


def test_an_unreachable_origin_is_no_survey(world, tmp_path):
    git(world.managed, "remote", "set-url", "origin", str(tmp_path / "gone.git"))
    assert world.check() is None
    old, release, error, _ = world.update()
    assert release is None and "ls-remote" in error


def test_an_update_that_breaks_after_the_stop_brings_the_services_back(world, monkeypatch):
    calls = []

    class Services:
        def stop_tracker(self): calls.append("stop_tracker")
        def stop_backend(self): calls.append("stop_backend")
        def start_backend(self, cb=None): calls.append("start_backend"); return True
        def start_tracker(self, cb=None): calls.append("start_tracker"); return True

    def broken():
        raise UnicodeDecodeError("cp1252", b"\x81", 0, 1, "undefined")
    monkeypatch.setattr(updater, "reset_to_release", broken)
    monkeypatch.setattr(updater, "_update_failed", lambda config, step, error: calls.append(("failed", step)))
    assert updater.perform_update(Services(), {}) == (False, [], False, True)
    assert calls == ["stop_tracker", "stop_backend", ("failed", "checkout"), "start_backend", "start_tracker"]


def test_git_output_is_read_as_utf8_whatever_the_locale(world, monkeypatch):
    world.commit("v2 — ünïcode\n")
    monkeypatch.setattr("locale.getpreferredencoding", lambda do_setlocale=True: "cp1252")
    world.use(world.seed)
    assert "ünïcode" in updater._git_cmd(["log", "-1", "--format=%s"]).stdout


# ── the first install (the bootstraps) ────────────────────────────────────

def _fresh(world, name):
    app = world.tmp / name
    git(world.tmp, "init", "-q", str(app))
    git(app, "config", "remote.origin.url", str(world.origin))
    signers = world.tmp / f"{name}-signers"
    signers.write_text(world.key.line() + "\n")
    return app, signers


def test_a_first_install_checks_out_the_newest_verified_release(world):
    first = world.commit("v2\n")
    world.tag("v0.2.0", first)
    second = world.commit("v3\n")
    world.tag("v0.3.0", second)
    world.tag("v0.4.0", world.commit("v4\n"), key=Key())     # not on the installer's list
    world.commit("v5, unreleased\n")
    app, signers = _fresh(world, "app")
    release = updater.install_release(str(app), str(signers))
    assert release.name == "v0.3.0"
    assert git(app, "rev-parse", "HEAD") == second
    assert git(app, "symbolic-ref", "--short", "HEAD") == "main"
    assert (app / "app.txt").read_text() == "v3\n"


def test_a_first_install_with_no_signed_release_takes_none(world):
    world.lightweight("v0.2.0", world.commit("v2\n"))
    app, signers = _fresh(world, "app")
    assert updater.install_release(str(app), str(signers)) is None
    assert not (app / "app.txt").exists()


def _payload_list(world, *keys):
    signers = world.tmp / "payload-signers"
    signers.write_text("".join(k.line() + "\n" for k in keys))
    return str(signers)


def test_a_newer_installer_moves_a_clone_past_a_rewritten_history(world, monkeypatch):
    tip = world.commit("v2\n", version="0.2.0")
    world.tag("v0.2.0", tip)
    world.update()
    git(world.seed, "checkout", "-q", "--orphan", "rewritten")
    rewritten = world.commit("v3, rewritten\n", signers=[world.key], version="0.3.0")
    world.tag("v0.3.0", rewritten)
    assert world.check().rewrite.name == "v0.3.0"
    monkeypatch.setattr(updater, "installer_version", lambda: (0, 3, 0))
    assert updater.install_release(str(world.managed), _payload_list(world, world.key)).name == "v0.3.0"
    assert world.head() == rewritten


def test_an_installer_no_newer_than_the_clone_never_moves_it(world, monkeypatch):
    """Round 2's A: an old installer's list must not undo a rotation the clone
    took, nor take it past a rewrite without a reinstall."""
    stolen, new = world.key, Key()
    rotating = world.commit("v2\n", signers=[new], version="0.3.0")
    world.tag("v0.3.0", rotating)                        # signed by the old key, rotates it out
    world.update()
    git(world.seed, "checkout", "-q", "--orphan", "thief")
    thief = world.commit("v9\n", signers=[stolen], version="0.9.0")
    world.tag("v0.9.0", thief, key=stolen)
    assert world.check().offered is None                 # the clone's own list refuses it
    for installer in ((0, 2, 0), (0, 3, 0)):             # an older installer, the same one
        monkeypatch.setattr(updater, "installer_version", lambda installer=installer: installer)
        assert updater.install_release(str(world.managed), _payload_list(world, stolen)) is None
        assert world.head() == rotating


def test_a_newer_installer_never_moves_a_clone_to_a_lower_version(world, monkeypatch):
    """Round 2's B: at a release whose own list drops its signer nothing HEAD
    holds verifies, and the version the tree states still holds the line."""
    k0, k1, k2 = world.key, Key(), Key()
    v3 = world.commit("v3\n", signers=[k1], version="0.3.0")
    world.tag("v0.3.0", v3, key=k0)
    world.update()
    v4 = world.commit("v4\n", signers=[k2], version="0.4.0")
    world.tag("v0.4.0", v4, key=k1)
    world.update()
    assert world.head() == v4
    monkeypatch.setattr(updater, "installer_version", lambda: (0, 5, 0))
    assert updater.install_release(str(world.managed), _payload_list(world, k0)) is None
    assert world.head() == v4


def test_the_release_a_clones_own_list_no_longer_verifies_is_up_to_date(world):
    k0, k1 = world.key, Key()
    v3 = world.commit("v3\n", signers=[k1], version="0.3.0")
    world.tag("v0.3.0", v3, key=k0)
    world.update()
    survey = world.check()
    assert (survey.held, survey.refused, survey.offered) == ("v0.3.0", [], None)


def test_a_first_install_runs_as_the_bootstraps_run_it(world):
    tip = world.commit("v2\n")
    world.tag("v0.2.0", tip)
    app, signers = _fresh(world, "app")
    command = [sys.executable, "-B", "-m", "desktop.updater", "install-release", str(app), str(signers)]
    result = subprocess.run(command, cwd=str(get_project_root()), capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
    assert git(app, "rev-parse", "HEAD") == tip
    git(world.seed, "push", "-q", "origin", ":refs/tags/v0.2.0")
    again = subprocess.run(command, cwd=str(get_project_root()), capture_output=True, text=True)
    assert again.returncode == 3, again.stdout + again.stderr


# ── a tree with edits of its own ──────────────────────────────────────────

@pytest.fixture
def released(world):
    tip = world.commit("v2\n")
    world.tag("v0.2.0", tip)
    return world, tip


def test_managed_tree_sets_its_edits_aside_and_moves(released, tmp_path):
    world, tip = released
    tree = world.managed
    (tree / "app.txt").write_text("v1 hot-patched\n")

    old_hash, release, error, set_aside = world.update(tree)

    assert error is None
    assert old_hash == world.base
    assert world.head(tree) == tip
    assert (tree / "app.txt").read_text() == "v2\n"
    assert set_aside.parent == get_data_dir() / "local-edits"
    # The patch is the edit itself: applied on the commit it was made on, it
    # gives the hot-patched file back.
    restore = tmp_path / "restore"
    git(tree, "worktree", "add", "-q", "--detach", str(restore), world.base)
    git(restore, "apply", str(set_aside))
    assert (restore / "app.txt").read_text() == "v1 hot-patched\n"


def test_working_tree_keeps_its_edits_and_refuses(released):
    world, _ = released
    tree = world.working
    (tree / "app.txt").write_text("v1 work in progress\n")

    old_hash, release, error, set_aside = world.update(tree)

    assert error == "the checkout has local modifications"
    assert (release, set_aside) == (None, None)
    assert world.head(tree) == world.base
    assert (tree / "app.txt").read_text() == "v1 work in progress\n"
    assert not (get_data_dir() / "local-edits").exists()


def test_working_tree_with_commits_of_its_own_is_never_moved(released):
    world, _ = released
    tree = world.working
    (tree / "app.txt").write_text("my own\n")
    git(tree, "commit", "-qam", "mine")
    mine = world.head(tree)
    survey = world.check(tree)
    assert survey.offered is None and survey.rewrite.name == "v0.2.0"
    world.update(tree)
    assert world.head(tree) == mine


def test_clean_managed_tree_sets_nothing_aside(released):
    world, tip = released
    old_hash, release, error, set_aside = world.update()
    assert (error, set_aside) == (None, None)
    assert world.head() == tip
    assert not (get_data_dir() / "local-edits").exists()


def test_a_refusal_is_said_in_the_dialog_only_when_nothing_is_offered():
    offered = updater.Release("v0.2.0", (0, 2, 0), "0" * 40)
    refused = [("v0.3.0", "the ssh-ed25519 key that signed is not listed for 'git'")]
    heading, lines = updater.survey_words(updater.Survey(offered=offered, refused=refused))
    assert heading == "Sautium 0.2.0 is available" and not any("v0.3.0" in line for line in lines)
    heading, lines = updater.survey_words(updater.Survey(refused=refused))
    assert heading == "No update was installed" and "v0.3.0" in lines[-1]


def test_a_tag_held_must_be_one_head_contains(world):
    """A tag that does not verify on a commit HEAD does NOT contain is a
    refusal, not held."""
    later = world.commit("v2\n")
    world.tag("v0.2.0", later, key=Key())
    survey = world.check()
    assert (survey.held, survey.refused[0][0]) == (None, "v0.2.0")


def test_held_tags_cost_one_git_process_however_many(world, monkeypatch):
    for i in range(30):
        world.tag(f"v9.0.{i}", world.base, key=Key())
    calls = []
    real = updater._git_cmd
    def counting(args, cwd=None, timeout=30):
        calls.append(args[0])
        return real(args, cwd, timeout)
    monkeypatch.setattr(updater, "_git_cmd", counting)
    survey = world.check()
    assert survey.held == "v9.0.29" and survey.refused == []
    assert calls.count("rev-list") == 1 and calls.count("merge-base") == 0


def test_the_updaters_name_filter_agrees_with_the_verifier():
    for name in (b"v999999.0.0", b"v1000000.0.0", b"v1.0.1234567", b"v0.2.0"):
        assert bool(updater._RELEASE_NAME.fullmatch(name)) == \
            (release_verify.parse_version(name.decode()) is not None)


def test_a_tree_that_states_no_version_reads_as_the_lowest():
    assert updater._stated_version("") == (0, 0, 0)
    assert updater._stated_version('VERSION = "0.3.1"\n') == (0, 3, 1)


def test_a_newer_installer_never_moves_a_clone_to_its_own_version(world, monkeypatch):
    tip = world.commit("v2\n", version="0.3.0")
    world.tag("v0.3.0", tip)
    world.update()
    git(world.seed, "checkout", "-q", "--orphan", "same")
    same = world.commit("v3 same version\n", signers=[world.key], version="0.3.0")
    world.tag("v0.3.0", same)                               # moved; the same version on another history
    monkeypatch.setattr(updater, "installer_version", lambda: (0, 4, 0))
    assert updater.install_release(str(world.managed), _payload_list(world, world.key)) is None
    assert world.head() == tip


def test_an_unreachable_origin_fails_a_reinstall_loudly(world, monkeypatch, tmp_path):
    world.commit("v2\n", version="0.2.0")
    git(world.managed, "pull", "-q", "--ff-only", "origin", "main")
    git(world.managed, "remote", "set-url", "origin", str(tmp_path / "gone.git"))
    monkeypatch.setattr(updater, "installer_version", lambda: (0, 3, 0))
    with pytest.raises(updater.UpdateError):
        updater.install_release(str(world.managed), _payload_list(world, world.key))
