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


def test_a_packed_ref_is_held_to_the_tags_own_name_as_a_loose_one_is(repo):
    """One rule for both: `refs/tags/v1.2.3` over a tag object called v9.9.9
    is no release 1.2.3, loose or after a pack-refs — whose peeled line
    names HEAD all the same."""
    _git(repo, "tag", "-a", "v9.9.9", "-m", "another name on HEAD")
    _git(repo, "update-ref", "refs/tags/v1.2.3", _git(repo, "rev-parse", "refs/tags/v9.9.9"))
    assert _release(repo) is None
    _git(repo, "pack-refs", "--all")
    assert "refs/tags/v1.2.3\n^" + _git(repo, "rev-parse", "HEAD") in (repo / ".git" / "packed-refs").read_text()
    assert _release(repo) is None
    # ... and the release's own tag is one, packed whole: its ref, its object
    _git(repo, "tag", "-d", "v1.2.3")
    _git(repo, "tag", "-a", "v1.2.3", "-m", "Sautium 1.2.3")
    _git(repo, "pack-refs", "--all")
    _git(repo, "repack", "-adq")
    assert _release(repo) == "v1.2.3"


# -- a checkout that is not as git writes it -------------------------------------
#
# The folder is the host's (a Docker node's is mounted beside its code) and
# is read where the backend is imported: nothing in it may cost a start.

@pytest.fixture
def said(monkeypatch, caplog):
    """The reader's one warning, counted."""
    import logging
    monkeypatch.setattr(updater, "_unreadable_said", False)
    caplog.set_level(logging.WARNING, logger=updater.__name__)
    return lambda: [r.getMessage() for r in caplog.records if "not readable as git writes it" in r.getMessage()]


def _says(repo, monkeypatch) -> tuple:
    """What a node on this checkout says it runs — computed, never raised."""
    import config
    monkeypatch.setenv("SAUTIUM_CHECKOUT", str(repo))
    monkeypatch.setattr(updater, "installed_build", lambda: None)
    return config.running_line(), config.running_version()


def test_packed_refs_that_are_no_text_cost_no_start(repo, monkeypatch, said):
    _git(repo, "tag", "-a", "v1.2.3", "-m", "Sautium 1.2.3")
    _git(repo, "pack-refs", "--all")
    head = _git(repo, "rev-parse", "HEAD")[:12]
    packed = repo / ".git" / "packed-refs"
    whole = packed.read_bytes()
    # rot before the lines that matter: they are still read
    packed.write_bytes(b"\xff\xfe\x00 not text \x80\n" + whole)
    assert _says(repo, monkeypatch) == (f"Sautium 1.2.3 ({head})", "1.2.3")
    # nothing but rot: no commit is known, and that is all
    packed.write_bytes(b"\xff\xfe" * 4000)
    assert _says(repo, monkeypatch) == ("Sautium (unknown build)", "unknown build")
    assert said() == []                                     # nothing git could not have written is claimed


def test_a_head_that_names_no_ref_is_not_followed(repo, tmp_path, monkeypatch, said):
    outside = tmp_path / "outside"
    outside.write_text("c" * 40 + "\n")
    for pointer in ("ref: ../../outside", f"ref: {outside}", "ref: refs/heads/../../../outside"):
        (repo / ".git" / "HEAD").write_text(pointer + "\n")
        assert updater.head_commit(repo) is None, pointer
        assert _says(repo, monkeypatch) == ("Sautium (unknown build)", "unknown build")
    assert len(said()) == 1 and "which is no ref" in said()[0]         # said once, however often it is met


def test_a_ref_of_fifty_megabytes_is_not_read(repo, monkeypatch, said):
    import tracemalloc
    (repo / ".git" / "refs" / "heads" / "main").write_bytes(b"a" * (50 * 2 ** 20))
    (repo / ".git" / "HEAD").write_bytes(b"ref: refs/heads/main\n")
    tracemalloc.start()
    try:
        assert _says(repo, monkeypatch) == ("Sautium (unknown build)", "unknown build")
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    assert peak < 2 ** 20, f"{peak} bytes held to read one ref"
    assert len(said()) == 1 and "over 4096 bytes" in said()[0]
    # a HEAD of that size, and one that is no text
    (repo / ".git" / "HEAD").write_bytes(b"b" * (50 * 2 ** 20))
    assert updater.head_commit(repo) is None
    (repo / ".git" / "HEAD").write_bytes(b"ref: refs/heads/\xff\xfe\n")
    assert updater.head_commit(repo) is None


def _index_of_the_packed_tag(repo):
    """The release's tag object in a pack, its ref loose: (the pack's index, the tag's id)."""
    _git(repo, "tag", "-a", "v1.2.3", "-m", "Sautium 1.2.3")
    _git(repo, "repack", "-adq")
    (index,) = (repo / ".git" / "objects" / "pack").glob("*.idx")
    index.chmod(0o644)
    assert _release(repo) == "v1.2.3"
    return index, bytes.fromhex(_git(repo, "rev-parse", "refs/tags/v1.2.3"))


@pytest.mark.parametrize("entry", ["the tag's own", "the last"])
def test_a_fanout_entry_of_four_billion_is_not_walked(repo, monkeypatch, said, entry):
    """A corrupt fanout made the lookup count to its entry: some four
    billion steps, where the backend is imported."""
    import time
    index, tag = _index_of_the_packed_tag(repo)
    head = _git(repo, "rev-parse", "HEAD")[:12]
    raw = bytearray(index.read_bytes())
    at = 8 + 4 * (tag[0] if entry == "the tag's own" else 255)
    raw[at:at + 4] = b"\xff\xff\xff\xff"
    index.write_bytes(bytes(raw))
    started = time.monotonic()
    assert _says(repo, monkeypatch) == (f"Sautium (commit {head})", f"commit {head}")
    assert time.monotonic() - started < 5
    assert len(said()) == 1 and "its fanout does not fit its length" in said()[0]


def test_an_object_a_corrupt_index_does_not_hold_is_not_counted_for(repo, said):
    """Where the hang was: the lookup counted to the fanout's entry while it
    did not find the name — for an object in another pack, or in none."""
    import time
    index, tag = _index_of_the_packed_tag(repo)
    raw = bytearray(index.read_bytes())
    raw[8 + 4 * tag[0]:12 + 4 * tag[0]] = b"\xff\xff\xff\xff"
    index.write_bytes(bytes(raw))
    absent = bytes([tag[0]]) + bytes(19)
    started = time.monotonic()
    assert updater._pack_offset(index, absent) is None
    assert time.monotonic() - started < 5
    assert len(said()) == 1


def test_a_corrupt_pack_beside_the_one_that_holds_the_tag_hides_nothing(repo, monkeypatch, said):
    _git(repo, "repack", "-adq")                            # the commit's pack
    (first,) = (repo / ".git" / "objects" / "pack").glob("*.idx")
    _git(repo, "tag", "-a", "v1.2.3", "-m", "Sautium 1.2.3")
    _git(repo, "repack", "-dq")                             # the tag's, beside it
    assert len(list((repo / ".git" / "objects" / "pack").glob("*.idx"))) == 2
    head = _git(repo, "rev-parse", "HEAD")[:12]
    tag = bytes.fromhex(_git(repo, "rev-parse", "refs/tags/v1.2.3"))
    first.chmod(0o644)
    raw = bytearray(first.read_bytes())
    raw[8 + 4 * tag[0]:12 + 4 * tag[0]] = b"\xff\xff\xff\xff"
    first.write_bytes(bytes(raw))
    assert _says(repo, monkeypatch) == (f"Sautium 1.2.3 ({head})", "1.2.3")


def test_a_truncated_pack_index_or_pack_costs_no_start(repo, monkeypatch, said):
    index, _tag = _index_of_the_packed_tag(repo)
    head = _git(repo, "rev-parse", "HEAD")[:12]
    whole = index.read_bytes()
    for cut in (4, 600, 8 + 1024, 8 + 1024 + 30, len(whole) - 41):
        index.write_bytes(whole[:cut])
        assert _says(repo, monkeypatch) == (f"Sautium (commit {head})", f"commit {head}"), cut
    index.write_bytes(whole)
    pack = index.with_suffix(".pack")
    pack.chmod(0o644)
    pack.write_bytes(pack.read_bytes()[:20])                # the index names an offset past its end
    assert _says(repo, monkeypatch) == (f"Sautium (commit {head})", f"commit {head}")
    index.write_bytes(b"\xfftOc\x00\x00\x00\x02" + b"\x00\x00\x00\x09" * 256)   # an index of nine objects, none there
    assert _says(repo, monkeypatch) == (f"Sautium (commit {head})", f"commit {head}")


def test_whatever_else_the_folder_holds_is_said_and_never_raised(repo, monkeypatch, said):
    """The readers' last line: a failure nobody listed is "not known" too."""
    def broken(root):
        raise RuntimeError("a folder nobody foresaw")
    monkeypatch.setattr(updater, "_git_dirs", broken)
    assert updater.head_commit(repo) is None and updater.head_release(repo) is None
    assert _says(repo, monkeypatch) == ("Sautium (unknown build)", "unknown build")
    assert len(said()) == 1 and "a folder nobody foresaw" in said()[0]


def test_a_tag_stored_as_a_delta_is_read_through_it():
    """Two tags of one commit share most of their bytes, and a repack may keep
    one as git's copy/insert instructions over the other."""
    base = b"object " + b"a" * 40 + b"\ntype commit\ntag v9.9.9\ntagger t <t@example.org> 0 +0000\n\nnine\n"
    target = base.replace(b"v9.9.9", b"v1.2.3").replace(b"nine", b"Sautium 1.2.3")
    cut = base.index(b"v9.9.9")
    tail = base.index(b"\ntagger")
    delta = (bytes([len(base), len(target)])
             + bytes([0x90, cut])                                   # copy base[0:cut]
             + bytes([6]) + b"v1.2.3"                               # insert the name
             + bytes([0x91, tail, base.index(b"nine") - tail])      # copy from "\ntagger" up to the message
             + bytes([14]) + b"Sautium 1.2.3\n")
    assert updater._delta_applied(base, delta) == target
    # ... and nothing is made of instructions that do not fit their base or their own length
    assert updater._delta_applied(base[:-1], delta) is None
    assert updater._delta_applied(base, delta[:-3]) is None
    assert updater._delta_applied(base, delta + bytes([0])) is None
    assert updater._delta_applied(base, bytes([len(base), 4, 0x91, 250, 200])) is None
    assert updater._varint(b"\x80\x80", 0) == (None, 2)
