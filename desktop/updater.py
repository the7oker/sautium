"""
Git-based auto-updater for Sautium: a launcher takes RELEASES — signed
annotated tags `v<major>.<minor>.<patch>` — never the tip of a branch.

A push to `main` reaches no installed launcher without the release key's
signature. The updater fetches origin's `v*` tags and verifies them with the
launcher's own Python (desktop/release_verify.py — the Windows installer's
MinGit has no `ssh-keygen`) along the release CHAIN (`walk_chain`): upward
from the installed tree, each release against the list of the release before
it — the installed tree's own list (`HEAD:desktop/release_signers`) only for
the first step, never a fetched tree's for its own release. What it offers is
the highest release so reached that the installed list verifies as well: a
rotation revokes the key it drops for every launcher below it, which takes
the rotating release first and the new key's releases at its next check. A
signed release off HEAD's history — a rewritten one — is refused, and only a
reinstall moves to it; two signed releases that continue the same one apart
are a fork, and nothing above their common release is taken. Nothing is
purged. The bootstraps' first clone walks the chain from the installer's own
release and list (`install-release`, below).

scripts/release.py, the maintainer's tool, walks the chain with the same
`walk_chain` from its recorded floor, so it can never accept a release
launchers refuse, nor refuse one they take.

After update: run pending DB migrations, restart backend+tracker. Dependency
installs are not done here — see perform_update.
"""

import hashlib
import itertools
import logging
import os
import re
import shutil
import stat
import struct
import subprocess
import sys
import zlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Callable, Tuple, List

from desktop import release_verify

logger = logging.getLogger(__name__)

# The repository moved on 2026-09-12 (history rewritten before going public).
# A checkout still pointing at the old address is switched to the new one
# before it fetches, so an installed node follows the move on its own — the
# last commit on the old repository carries this very code. Any other origin
# (a fork, a mirror) is left alone.
ORIGIN_MOVES = {
    "github.com/the7oker/ai.djai": "https://github.com/the7oker/sautium.git",
}

# A release is fetched only once its tag has verified. Origin's tag names are
# listed first (`ls-remote`), and only names of the release form go any
# further: whatever else a pusher invents — a name this filesystem cannot
# store, one that is not UTF-8 — never reaches a fetch, where one bad name
# fails it for every release beside it. The tag objects are then fetched into
# a PROBE repository inside our git dir, shallow and without trees
# (`--depth=1 --filter=tree:0`: the tag and its commit, a few hundred bytes)
# and judged there; only a release that verifies is fetched into our own
# repository, history and all, under a namespace of our own (refs/tags stays
# whoever works in the tree's). A pushed tag over a huge history costs a
# launcher those few hundred bytes.
RELEASE_REFS = "refs/sautium/releases/"
PROBE_DIR = "sautium-release-probe"
SIGNERS_PATH = "desktop/release_signers"
_RELEASE_NAME = re.compile(rb"v(0|[1-9][0-9]{0,5})\.(0|[1-9][0-9]{0,5})\.(0|[1-9][0-9]{0,5})")
# A tag object is a few hundred bytes; anything far larger is no release and
# is never read into memory.
_TAG_OBJECT_LIMIT = 64 * 1024

# v0.1.0 and v0.1.1 were published as lightweight, unsigned tags before
# releases were signed. Nothing at or below this version is a release.
FLOOR = (0, 1, 1)


class UpdateError(RuntimeError):
    """git failed on the way to origin's releases (no network, no origin)."""


@dataclass(frozen=True)
class Release:
    name: str                       # the signed tag's own name, `v0.2.0`
    version: Tuple[int, int, int]
    commit: str

    @property
    def label(self) -> str:
        return "Sautium " + self.name[1:]


@dataclass
class Survey:
    """What origin's releases are to this checkout.

    `installed`: the highest signed release HEAD already contains whose tree
    states its version (None before the first one). `held`: the highest
    release tag HEAD contains that is no installed release — signed by a key
    HEAD's own list does not name (a release made before scripts/release.py
    refused one whose list drops its signer, a stranger's tag on an old
    commit), or on a commit that states another version or one above HEAD's
    (a listed key's tag under a higher name on an older commit) — said as
    "up to date", never a
    refusal, never a floor for the walk. `offered`: the highest release the
    chain reaches above it that the installed list verifies too — what an
    update moves to. `rewrite`: a signed release above it that does not
    descend from HEAD (a rewritten history, which only a reinstall reaches).
    `refused`: `v*` tags above all of them this checkout does not take —
    unsigned, signed by a key their place in the chain does not list,
    renamed, broken, a fork — as (tag, why), the newest first. `fork`: the
    newest fork above any offer or rewrite, (that tag, the release it
    competes with): two signed continuations of the same release, of which
    the launcher cannot tell the owner's."""
    installed: Optional[Release] = None
    offered: Optional[Release] = None
    rewrite: Optional[Release] = None
    refused: List[Tuple[str, str]] = field(default_factory=list)
    held: Optional[str] = None
    fork: Optional[Tuple[str, str]] = None


# The update dialog's words (desktop/launcher.py shows them) — drafts for
# the owner's words review (SIGN-1).
def survey_words(survey: Survey) -> Tuple[str, List[str]]:
    """(heading, paragraphs) for a survey that has something to say."""
    paragraphs = []
    if survey.offered:
        heading = f"{survey.offered.label} is available"
        paragraphs.append("Services will be briefly restarted during the update.")
    elif survey.rewrite:
        heading = f"{survey.rewrite.label} needs a reinstall"
    else:
        heading = "No update was installed"
    if survey.rewrite:
        paragraphs.append(
            f"{survey.rewrite.label} rewrites the history this copy was installed from, so it "
            "cannot be installed as an update. Reinstalling Sautium moves to it.")
    # A refusal above an offer is said in the log only (D5).
    if survey.fork and not survey.offered:
        paragraphs.append(
            f"Releases {survey.fork[0]} and {survey.fork[1]} continue the same release in two "
            "different ways, so neither was installed.")
    elif survey.refused and not survey.offered:
        paragraphs.append(
            f"A release tagged {survey.refused[0][0]} does not carry a signature this copy "
            "trusts, so it was not installed.")
    return heading, paragraphs


def survey_button(survey: Survey) -> Optional[str]:
    """The update button's label while a survey waits on the person."""
    if survey.offered:
        return f"Update to {survey.offered.name}"
    if survey.rewrite:
        return "Reinstall Needed"
    return None


NOTHING_SIGNED_YET = "No signed release has been published yet."


def _git_cmd(args: list, cwd: Optional[str] = None, timeout: int = 30) -> subprocess.CompletedProcess:
    """Run a git command in `cwd`, by default our own tree."""
    if cwd is None:
        from desktop.utils import get_project_root
        cwd = str(get_project_root())
    # UTF-8 whatever the locale (git writes commit subjects in it): Windows'
    # ANSI code page cannot decode every byte git may print.
    kwargs = {
        "capture_output": True,
        "text": True,
        "encoding": "utf-8",
        "errors": "replace",
        "timeout": timeout,
        "cwd": cwd,
    }
    if sys.platform == "win32":
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    try:
        return subprocess.run(["git"] + args, **kwargs)
    except subprocess.TimeoutExpired as exc:
        raise UpdateError(f"git {args[0]} gave no answer in {timeout} s") from exc


def _git_bytes(args: list, cwd: Optional[str] = None, stdin: bytes = b"",
               timeout: int = 60) -> subprocess.CompletedProcess:
    """A git command whose output stays the bytes git wrote: a signature
    covers a tag object byte for byte, and a text decode may alter it."""
    if cwd is None:
        from desktop.utils import get_project_root
        cwd = str(get_project_root())
    kwargs = {"capture_output": True, "timeout": timeout, "cwd": cwd, "input": stdin}
    if sys.platform == "win32":
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    try:
        return subprocess.run(["git"] + args, **kwargs)
    except subprocess.TimeoutExpired as exc:
        raise UpdateError(f"git {args[0]} gave no answer in {timeout} s") from exc


def _rev(ref: str, cwd: Optional[str] = None) -> Optional[str]:
    result = _git_cmd(["rev-parse", "--verify", "--quiet", ref], cwd)
    return result.stdout.strip() if result.returncode == 0 else None


def _ancestry(commit: str, of: str, cwd: Optional[str] = None) -> Optional[bool]:
    """Whether `commit` is `of` or an ancestor of it; None when git cannot
    tell (an object that is not a commit, or is not here)."""
    code = _git_cmd(["merge-base", "--is-ancestor", commit, of], cwd).returncode
    return {0: True, 1: False}.get(code)


def _migrate_origin(cwd: Optional[str] = None) -> None:
    """Point origin at the repository's current address if it still names a
    former one. Runs before every fetch; a no-op for every other origin."""
    result = _git_cmd(["remote", "get-url", "origin"], cwd)
    if result.returncode != 0:
        return
    url = result.stdout.strip()
    for old, new in ORIGIN_MOVES.items():
        if old in url and url != new:
            moved = _git_cmd(["remote", "set-url", "origin", new], cwd)
            if moved.returncode == 0:
                logger.info(f"origin moved: {url} -> {new}")
            else:
                logger.warning(f"origin move failed: {moved.stderr.strip()}")
            return


def installed_signers(cwd: Optional[str] = None) -> List[release_verify.Signer]:
    """The allowed signers HEAD was committed with — not the working copy's
    file, not a fetched tree's: the keys that may move this checkout are the
    ones it already trusts."""
    result = _git_bytes(["show", f"HEAD:{SIGNERS_PATH}"], cwd)
    if result.returncode != 0:
        logger.warning(f"HEAD has no {SIGNERS_PATH} — no release can verify")
        return []
    return release_verify.parse_allowed_signers(
        result.stdout.decode("utf-8", "replace"), f"HEAD:{SIGNERS_PATH}")


def signers_at(commit: str, cwd: Optional[str] = None) -> List[release_verify.Signer]:
    """The allowed signers a release's own tree carries — the keys that may
    sign the release after it."""
    result = _git_bytes(["show", f"{commit}:{SIGNERS_PATH}"], cwd)
    if result.returncode != 0:
        return []
    return release_verify.parse_allowed_signers(
        result.stdout.decode("utf-8", "replace"), f"{commit[:12]}:{SIGNERS_PATH}")


def _objects(refs: List[str], git: List[str], cwd: Optional[str]) -> dict:
    """{ref: (object id, object type, raw bytes or None when larger than any
    tag object)}: their sizes first (`--batch-check`), then only the small
    ones' bytes (`--batch`). `git` carries the options that name the
    repository."""
    def run(mode, wanted):
        result = _git_bytes(git + ["cat-file", mode], cwd, "".join(f"{ref}\n" for ref in wanted).encode())
        if result.returncode != 0:
            raise UpdateError(f"git cat-file failed: {result.stderr.decode(errors='replace').strip()}")
        return result.stdout
    objects, small = {}, []
    for ref, line in zip(refs, run("--batch-check", refs).splitlines()):
        header = line.split()
        if len(header) != 3:            # "<ref> missing"
            objects[ref] = ("", "missing", b"")
            continue
        objects[ref] = (header[0].decode(), header[1].decode(), None)
        if int(header[2]) <= _TAG_OBJECT_LIMIT:
            small.append(ref)
    out, at = run("--batch", small) if small else b"", 0
    for ref in small:
        end = out.index(b"\n", at)
        size = int(out[at:end].split()[2])
        at = end + 1
        objects[ref] = objects[ref][:2] + (out[at:at + size],)
        at += size + 1
    return objects


def _remote_releases(cwd: Optional[str]) -> List[Tuple[Tuple[int, int, int], str, bool]]:
    """Origin's tags of the release form above the floor, newest first, each
    with whether it is annotated (ls-remote prints its peeled `^{}` line):
    a lightweight tag is no release and is never fetched. Names are matched
    as bytes: nothing else a pusher names is ever decoded."""
    result = _git_bytes(["ls-remote", "origin", "refs/tags/v*"], cwd, timeout=120)
    if result.returncode != 0:
        raise UpdateError(f"git ls-remote failed: {result.stderr.decode(errors='replace').strip()}")
    names, annotated = [], set()
    for line in result.stdout.splitlines():
        _oid, _, ref = line.partition(b"\t")
        if not ref.startswith(b"refs/tags/"):
            continue
        name = ref[len(b"refs/tags/"):]
        if name.endswith(b"^{}"):
            annotated.add(name[:-3])
        elif _RELEASE_NAME.fullmatch(name):
            names.append(name)
    found = []
    for name in names:
        version = release_verify.parse_version(name.decode())
        if version > FLOOR:
            found.append((version, name.decode(), name in annotated))
    found.sort(reverse=True)
    return found


def _probe_repository(cwd: Optional[str]) -> List[str]:
    """The git options that address the probe, created (or made again) when
    it is not a repository — every probe command names it by --git-dir, so
    none can ever fall through to the launcher's own repository."""
    git_dir = _git_cmd(["rev-parse", "--absolute-git-dir"], cwd)
    url = _git_cmd(["remote", "get-url", "origin"], cwd)
    if git_dir.returncode != 0 or url.returncode != 0:
        raise UpdateError(f"not a repository with an origin: {git_dir.stderr.strip() or url.stderr.strip()}")
    probe = Path(git_dir.stdout.strip()) / PROBE_DIR
    git = ["--git-dir", str(probe)]
    if _git_cmd(git + ["rev-parse", "--is-bare-repository"], cwd).stdout.strip() != "true":
        if probe.exists():
            # It holds nothing but tag objects, fetched again below.
            # git writes its objects read-only, which Windows will not delete.
            for item in probe.rglob("*"):
                if item.is_file():
                    os.chmod(item, stat.S_IREAD | stat.S_IWRITE)
            shutil.rmtree(probe)
        made = _git_cmd(["init", "--quiet", "--bare", str(probe)], cwd)
        if made.returncode != 0:
            raise UpdateError(f"git init of the release probe failed: {made.stderr.strip()}")
    # A partial-clone remote, so the server may leave trees and history out.
    for key, value in (("core.repositoryformatversion", "1"), ("extensions.partialClone", "origin"),
                       ("remote.origin.url", url.stdout.strip()), ("remote.origin.promisor", "true"),
                       ("remote.origin.partialclonefilter", "tree:0")):
        _git_cmd(git + ["config", key, value], cwd)
    return git


def _probe(names: List[str], cwd: Optional[str]) -> dict:
    """{name: (object id, type, raw bytes or None)} of origin's annotated
    release tags, fetched into the probe: the tag objects and their commits,
    no trees, no history. What a tag object names is still sent whole — a
    pusher's tag on a large blob costs that blob once, kept in the probe, and
    the long timeout lets a slow line finish it rather than fail every check."""
    git = _probe_repository(cwd)
    refspecs = "".join(f"+refs/tags/{name}:refs/tags/{name}\n" for name in names).encode()
    fetched = _git_bytes(git + ["fetch", "--quiet", "--no-tags", "--depth=1", "--filter=tree:0", "--stdin",
                                "origin"], cwd, refspecs, timeout=1800)
    if fetched.returncode != 0:
        raise UpdateError(f"git fetch of the release tags failed: {fetched.stderr.decode(errors='replace').strip()}")
    objects = _objects([f"refs/tags/{name}" for name in names], git, cwd)
    return {name: objects[f"refs/tags/{name}"] for name in names}


def _fetch_release(name: str, oid: str, cwd: Optional[str]) -> bool:
    """Fetch a release that verified in the probe, history and all; False
    when origin's tag is no longer the object that verified."""
    result = _git_cmd(["fetch", "--quiet", "--no-tags", "origin", f"+refs/tags/{name}:{RELEASE_REFS}{name}"],
                      cwd, timeout=1800)
    if result.returncode != 0:
        raise UpdateError(f"git fetch of {name} failed: {result.stderr.strip()}")
    return _rev(f"{RELEASE_REFS}{name}", cwd) == oid


def _forget_releases_but(kept: set, cwd: Optional[str]) -> None:
    """Drop our refs of releases this survey did not fetch (gone from origin,
    no longer verifying, below the installed one): HEAD keeps what it needs."""
    listing = _git_cmd(["for-each-ref", "--format=%(refname)", RELEASE_REFS], cwd)
    stale = [ref for ref in listing.stdout.split() if ref[len(RELEASE_REFS):] not in kept]
    if stale:
        _git_bytes(["update-ref", "--stdin"], cwd, "".join(f"delete {ref}\n" for ref in stale).encode())


def _history(head: str, cwd: Optional[str]) -> set:
    """Every commit HEAD contains, in one `rev-list`: a pusher's thousand tags
    on old commits cost one git process, not a thousand."""
    result = _git_cmd(["rev-list", head], cwd, timeout=120)
    if result.returncode != 0:
        raise UpdateError(f"git rev-list failed: {result.stderr.strip()}")
    return set(result.stdout.split())


def admitted(kind: str, raw: Optional[bytes]) -> Optional[str]:
    """Why an annotated tag's object is no candidate at all, or None — the one
    gate launchers and scripts/release.py both apply before a signature is
    read."""
    if raw is None:
        return f"a {kind} too large to be a release tag"
    if kind != "tag":
        return f"a tag of a {kind}, not a signed release"
    return None


def self_stated(commit: str, cwd: Optional[str] = None) -> str:
    """The text of `commit`'s desktop/build_common.py ("" when it has none)."""
    return _git_cmd(["show", f"{commit}:desktop/build_common.py"], cwd).stdout


def _tagged_commit(raw: bytes) -> Optional[str]:
    """The commit a tag object names, read without trusting it."""
    try:
        headers = release_verify.tag_headers(raw)
    except release_verify.VerifyError:
        return None
    return headers["object"] if headers["type"] == "commit" else None


@dataclass(frozen=True)
class Link:
    """A step of the release chain: a release, or the walk's start (HEAD, an
    installer's list, the maintainer's floor), with the list its tree carries
    — the keys that may sign the next step. A start with no commit (a first
    install, a root derived from a list) is continued by whatever verifies
    first."""
    release: Optional[Release]
    commit: Optional[str]
    signers: Tuple[release_verify.Signer, ...]
    trusted_at_start: bool = False      # the start's own list verifies it too

    @property
    def label(self) -> str:
        return self.release.name if self.release else "the release this copy holds"


@dataclass
class Chain:
    """What `walk_chain` reached. `links[0]` is the start; `cut` is the last
    link an offer may come from when a fork stands at it; each refusal is
    (version, tag, why), each fork (version, tag, the link it continues, the
    release beside it, why); `rewrites` are releases a link's list
    verifies that descend from no link — off the start's history."""
    links: List[Link]
    cut: Optional[int] = None
    refused: List[Tuple[Tuple[int, int, int], str, str]] = field(default_factory=list)
    forks: List[Tuple[Tuple[int, int, int], str, int, str, str]] = field(default_factory=list)
    rewrites: List[Release] = field(default_factory=list)

    def reach(self) -> List[Link]:
        """The links above the start that an offer may come from."""
        return self.links[1:None if self.cut is None else self.cut + 1]


def walk_chain(start: Link, candidates: List[Tuple[Tuple[int, int, int], str, str, bytes]],
               cwd: Optional[str] = None,
               fetch: Optional[Callable[[str, str], bool]] = None) -> Chain:
    """Walk origin's releases upward from `start`, the oldest version first —
    the one rule launchers and scripts/release.py both follow.

    `candidates` are (version, tag, tag object id, the tag object) above the
    start. Each is verified against the list of the newest link it descends
    from, never against the start's alone: a key a release drops can sign
    nothing above that release, for a launcher still below it as well. A
    release that verifies there, on the newest link, continues the chain; one
    that verifies there on an OLDER link is a fork — two signed continuations
    of that link, of which no launcher can tell the owner's (a thief's can
    take any version, a lower one too) — and nothing above that link may be
    offered (`cut`) until one of the two tags is gone. A release no link's
    list verifies is never fetched; one that verifies against some link's is
    fetched by `fetch(tag, object id)` (False: origin's tag moved meanwhile)
    before its history is read."""
    chain = Chain([start])
    for version, name, oid, raw in sorted(candidates):
        links = chain.links
        tags, errors = {}, {}
        for at, link in enumerate(links):
            try:
                tags[at] = release_verify.verify_tag(raw, name, list(link.signers))
            except release_verify.VerifyError as exc:
                errors[at] = str(exc)
        if not tags:
            chain.refused.append((version, name, errors[len(links) - 1]))
            continue
        commit = next(iter(tags.values())).commit
        if fetch is not None and not fetch(name, oid):
            chain.refused.append((version, name, "it moved on origin during the check"))
            continue
        on, unreadable = None, False
        for at in range(len(links) - 1, -1, -1):
            descends = True if links[at].commit is None else _ancestry(links[at].commit, commit, cwd)
            if descends is None:
                unreadable = True
                break
            if descends:
                on = at
                break
        release = Release(name, version, commit)
        if unreadable:
            chain.refused.append((version, name, "its commit cannot be read"))
        elif on is None:
            chain.rewrites.append(release)
        elif on not in tags:
            chain.refused.append((version, name, errors[on]))
        elif on == len(links) - 1:
            links.append(Link(release, commit, tuple(signers_at(commit, cwd)), 0 in tags))
        else:
            other = links[on + 1].release.name
            chain.forks.append((version, name, on, other,
                                f"it does not descend from {other}, which continues {links[on].label} too"))
            chain.cut = on if chain.cut is None else min(chain.cut, on)
    return chain


def survey(signers: List[release_verify.Signer], head: Optional[str],
           cwd: Optional[str] = None, min_version: Tuple[int, int, int] = (0, 0, 0)) -> Survey:
    """Judge origin's releases for a checkout at `head` that trusts `signers`
    (its own list); UpdateError when origin cannot be read.

    With a `head`, the walk starts there, above the first signed release HEAD
    contains: nothing at or below the installed one can be an update, so a
    version is never taken backwards. Without one — an installer's first
    clone — it starts at `signers` alone and at `min_version`, the
    installer's own release."""
    remote = [entry for entry in _remote_releases(cwd) if entry[0] >= min_version]
    annotated = [name for _, name, signed in remote if signed]
    objects = _probe(annotated, cwd) if annotated else {}
    return _judge(signers, head, cwd, remote, objects,
                  fetch=lambda name, oid: _fetch_release(name, oid, cwd), forget=True)


def survey_fetched(signers: List[release_verify.Signer], head: str, cwd: str) -> Survey:
    """`survey` over the release tags already fetched into RELEASE_REFS,
    touching no network and writing nothing: a Docker node's host agent
    (scripts/host_agent.py) fetches them, then runs this from the installed
    commit's own desktop/ in a container with no network and the checkout's
    .git read-only. The same judgement, the same walk."""
    listing = _git_cmd(["for-each-ref", "--format=%(refname)", RELEASE_REFS], cwd)
    if listing.returncode != 0:
        raise UpdateError(f"git for-each-ref failed: {listing.stderr.strip()}")
    names = [ref[len(RELEASE_REFS):] for ref in listing.stdout.split()]
    names = [name for name in names if _RELEASE_NAME.fullmatch(name.encode())]
    objects = _objects([f"{RELEASE_REFS}{name}" for name in names], [], cwd) if names else {}
    objects = {name: objects[f"{RELEASE_REFS}{name}"] for name in names}
    remote = sorted(((release_verify.parse_version(name), name, objects[name][1] == "tag")
                     for name in names if release_verify.parse_version(name) > FLOOR), reverse=True)
    return _judge(signers, head, cwd, remote, objects, fetch=None, forget=False)


def _judge(signers: List[release_verify.Signer], head: Optional[str], cwd: Optional[str],
           remote: List[Tuple[Tuple[int, int, int], str, bool]], objects: dict,
           fetch: Optional[Callable[[str, str], bool]], forget: bool) -> Survey:
    """The survey's judgement of `remote` — (version, tag, annotated), the
    newest first — whose tag objects are `objects`: the installed release,
    then the walk above it."""
    found, refused, candidates = Survey(), [], []
    history = _history(head, cwd) if head else set()
    # No release HEAD holds is above the version HEAD's own tree states (on a
    # managed install, exactly the installed one's). A HEAD that states none
    # (a working tree's own edit) sets no ceiling: with one of (0, 0, 0) every
    # release it holds would be held, and a lower version offered.
    ceiling = _stated_version(self_stated(head, cwd)) if head else (0, 0, 0)
    ceiling = None if ceiling == (0, 0, 0) else ceiling
    for version, name, signed in remote:            # the newest first
        if not signed:
            refused.append((version, name, "a lightweight tag, not a signed release"))
            continue
        oid, kind, raw = objects[name]
        why = admitted(kind, raw)
        if why:
            refused.append((version, name, why))
            continue
        if _tagged_commit(raw) in history:
            # The installed release ends the read, so it must be one: a
            # release's tree states its own version (scripts/release.py
            # refuses any other), and none is above what HEAD states. A key's
            # tag on an older commit of HEAD's — a stolen key's v9.0.0 on the
            # installed commit itself, or on a merged commit that once said
            # 9.0.0 — is no installed release, and would otherwise hide every
            # release below it from the walk, the rotation that drops that key
            # among them.
            try:
                tag = release_verify.verify_tag(raw, name, signers)
                stated = _stated_version(self_stated(tag.commit, cwd))
                why = None if stated == version and (ceiling is None or version <= ceiling) else \
                    "its tree states another version, or one above HEAD's"
            except release_verify.VerifyError as exc:
                why = str(exc)
            if why:
                logger.debug(f"tag {name} held: HEAD contains it, and it is no release here ({why})")
                found.held = found.held or name
                continue
            found.installed = Release(name, version, tag.commit)
            break
        candidates.append((version, name, oid, raw))

    chain = walk_chain(Link(None, head, tuple(signers)), candidates, cwd, fetch=fetch)
    trusted = [link.release for link in chain.reach() if link.trusted_at_start]
    found.offered = trusted[-1] if trusted else None
    above = found.offered.version if found.offered else (0, 0, 0)
    rewrites = [release for release in chain.rewrites if release.version > above]
    found.rewrite = max(rewrites, key=lambda release: release.version) if rewrites else None
    if found.rewrite:
        above = found.rewrite.version
    refused += chain.refused + [(version, name, why) for version, name, _, _, why in chain.forks]
    found.refused = [(name, why) for version, name, why in sorted(refused, reverse=True) if version > above]
    forks = sorted((version, name, other) for version, name, _, other, _ in chain.forks if version > above)
    if forks:
        found.fork = forks[-1][1:]
    if forget:
        _forget_releases_but({link.release.name for link in chain.links[1:]} |
                             {release.name for release in chain.rewrites}, cwd)
    for _, name, _, other, _ in chain.forks:
        logger.warning(f"release tags {name} and {other} fork: nothing above what both continue is taken "
                       "until one of them is gone from origin")
    if found.refused:
        name, why = found.refused[0]
        logger.warning(f"{len(found.refused)} release tag(s) refused; the newest, {name}: {why}")
    return found


def get_project_root() -> Path:
    """Get the project root directory (git repo root)."""
    from desktop.utils import get_project_root as _get_root
    return _get_root()


def is_managed_install() -> bool:
    """Whether OUR tree is the one a carrier installed — `<data dir>/app`
    (desktop/macos/bootstrap.py, desktop/windows/bootstrap.py). The updater
    owns that tree and nobody works in it. Any other checkout is a working
    tree: the developer's runs this launcher as a test stand."""
    from desktop.config_manager import get_data_dir
    return get_project_root().resolve() == (get_data_dir() / "app").resolve()


def installed_build() -> Optional[str]:
    """The build id of a packaged install (a bootstrap that could not clone
    writes it beside the copy it unpacks — desktop/macos/bootstrap.py,
    desktop/windows/bootstrap.py), or None for a git checkout. A packaged
    tree has no remote to compare against; it stays as it is until another
    installer runs, whose bootstrap tries the clone again."""
    return _small(get_project_root() / ".sautium_build") or None


_HEX40 = re.compile(r"[0-9a-f]{40}")

# What a node reads of its checkout by hand, to say what it runs. The folder
# is not this process's to trust — a Docker node's is the host's, mounted
# beside its code, and a card rots — and it is read where the backend is
# imported (the API's version): every read is bounded, and whatever is not
# there as git writes it is "not known", said once at warning and never
# raised. A label must not cost a start.
_SMALL_FILE = 4096                  # HEAD, a ref, a `.git` pointer, commondir, the build stamp
_PACKED_REFS_LIMIT = 16 * 2 ** 20   # some 200 000 refs
_PACK_INDEXES = 256                 # packs looked into for one tag object
_INDEX_HEAD = 8 + 256 * 4           # a version-2 pack index's magic and fanout
_INDEX_TAIL = 2 * 20                # ... and its two trailing checksums
_DELTA_DEPTH = 8                    # a tag stored as a delta of a delta of …: followed this far
_unreadable_said = False


def _unreadable(what, why) -> None:
    global _unreadable_said
    if _unreadable_said:
        return
    _unreadable_said = True
    logger.warning(f"the checkout is not readable as git writes it ({what}: {why}) — this node "
                   "says no release or commit it cannot read there")


def _small(path: Path) -> Optional[str]:
    """The text of a file git keeps one line in — None where there is none,
    and (said) where it is longer than any git writes, or no text."""
    try:
        with open(path, "rb") as f:
            raw = f.read(_SMALL_FILE + 1)
    except (FileNotFoundError, NotADirectoryError, IsADirectoryError):
        return None
    except OSError as exc:
        _unreadable(path, exc)
        return None
    if len(raw) > _SMALL_FILE:
        _unreadable(path, f"over {_SMALL_FILE} bytes")
        return None
    try:
        return raw.decode("utf-8").strip()
    except UnicodeDecodeError as exc:
        _unreadable(path, exc)
        return None


def current_commit() -> Optional[str]:
    """HEAD of our checkout — the build identity a support report and a
    backup manifest carry. Read from .git by hand, never by spawning git:
    a launcher-spawned CLI that shelled out here hung forever on Windows
    (2026-09-14 — git's own child kept the pipes open past run()'s timeout,
    and communicate() waited on them for good). None for a packaged tree
    (see installed_build)."""
    return head_commit(get_project_root())


def _git_dirs(root: Path) -> Optional[Tuple[Path, Path]]:
    """(`root`'s git dir, the repository's common dir): a linked worktree's
    `.git` file and `commondir` are followed."""
    git_dir = root / ".git"
    if git_dir.is_file():
        text = _small(git_dir)
        if text is None or not text.startswith("gitdir:"):
            return None
        git_dir = Path(text[len("gitdir:"):].strip())
        if not git_dir.is_absolute():
            git_dir = (root / git_dir).resolve()
    common = _small(git_dir / "commondir")
    return git_dir, (git_dir / common).resolve() if common else git_dir


def _packed_ref(common: Path, ref: str) -> Tuple[Optional[str], Optional[str]]:
    """`ref`'s line in packed-refs, and the peeled line under it — read as
    bytes, line by line, up to _PACKED_REFS_LIMIT."""
    want = ref.encode("utf-8")
    try:
        f = open(common / "packed-refs", "rb")
    except (FileNotFoundError, NotADirectoryError, IsADirectoryError):
        return None, None
    with f:
        read, found = 0, None
        while True:
            line = f.readline(_SMALL_FILE)
            read += len(line)
            if found is not None:
                peeled = line[1:].strip().decode("ascii", "replace") if line.startswith(b"^") else ""
                return found, peeled if _HEX40.fullmatch(peeled) else None
            if not line:
                return None, None
            if read > _PACKED_REFS_LIMIT:
                _unreadable(common / "packed-refs", f"over {_PACKED_REFS_LIMIT} bytes")
                return None, None
            parts = line.split()
            if len(parts) == 2 and parts[1] == want:
                oid = parts[0].decode("ascii", "replace")
                if _HEX40.fullmatch(oid):
                    found = oid


def _ref_oid(git_dir: Path, common: Path, ref: str) -> Tuple[Optional[str], Optional[str]]:
    """(the object `ref` names, the commit a packed ref's annotated tag peels
    to) from a loose ref or packed-refs — (None, None) where there is none,
    and for a name that is no ref's: HEAD may say anything, and only a path
    under `refs/` is read."""
    if not ref.startswith("refs/") or ".." in ref.split("/") or "\0" in ref:
        _unreadable("HEAD", f"it names {ref[:80]!r}, which is no ref")
        return None, None
    for candidate in (git_dir / ref, common / ref):
        value = _small(candidate)
        if value is not None and _HEX40.fullmatch(value):
            return value, None
    return _packed_ref(common, ref)


def _head_oid(root: Path) -> Optional[str]:
    dirs = _git_dirs(root)
    if dirs is None:
        return None
    git_dir, common = dirs
    head = _small(git_dir / "HEAD")
    if head is None:
        return None
    if not head.startswith("ref:"):
        return head if _HEX40.fullmatch(head) else None
    return _ref_oid(git_dir, common, head[len("ref:"):].strip())[0]


def head_commit(root: Path) -> Optional[str]:
    """The first 12 hex digits of HEAD under `root`, from the files git keeps
    them in: a detached HEAD, a loose ref, a packed ref; a linked worktree's
    `.git` file and `commondir` are followed. None where there is no
    checkout, and where it cannot be read (_unreadable): never an error."""
    try:
        head = _head_oid(root)
    except Exception as exc:
        _unreadable("HEAD", repr(exc))
        return None
    return head[:12] if head else None


def _pack_offset(index: Path, oid: bytes) -> Optional[int]:
    """Where a pack holds the object `oid`, from its version-2 index: a
    binary search of the names its fanout brackets, as git's own — and only
    in an index as long as its own count of objects says it is."""
    with open(index, "rb") as f:
        size = os.fstat(f.fileno()).st_size
        head = f.read(_INDEX_HEAD)
        if len(head) != _INDEX_HEAD or head[:8] != b"\377tOc\0\0\0\2":
            return None
        fanout = struct.unpack(">256I", head[8:])
        low, high, total = (fanout[oid[0] - 1] if oid[0] else 0), fanout[oid[0]], fanout[255]
        if not low <= high <= total or _INDEX_HEAD + 28 * total + _INDEX_TAIL > size:
            _unreadable(index, "its fanout does not fit its length")
            return None
        at = None
        while low < high:
            middle = (low + high) // 2
            f.seek(_INDEX_HEAD + 20 * middle)
            name = f.read(20)
            if name == oid:
                at = middle
                break
            if name < oid:
                low = middle + 1
            else:
                high = middle
        if at is None:
            return None
        f.seek(_INDEX_HEAD + 24 * total + 4 * at)
        (offset,) = struct.unpack(">I", f.read(4))
        if offset & 0x80000000:
            large = _INDEX_HEAD + 28 * total + 8 * (offset & 0x7FFFFFFF)
            if large + 8 > size - _INDEX_TAIL:
                _unreadable(index, "an offset past its end")
                return None
            f.seek(large)
            (offset,) = struct.unpack(">Q", f.read(8))
        return offset


def _varint(data: bytes, at: int) -> Tuple[Optional[int], int]:
    """A little-endian base-128 number of a pack (an entry's or a delta's
    size) from `data[at:]`: (it, where the next field starts) — (None, at)
    where the data ends inside it."""
    value = shift = 0
    while at < len(data) and shift <= 63:
        byte = data[at]
        at += 1
        value |= (byte & 0x7F) << shift
        shift += 7
        if not byte & 0x80:
            return value, at
    return None, at


def _delta_applied(base: bytes, delta: bytes) -> Optional[bytes]:
    """`delta` over `base` (git's copy/insert instructions), or None where it
    is not one that makes an object of at most _TAG_OBJECT_LIMIT bytes."""
    source, at = _varint(delta, 0)
    target, at = _varint(delta, at)
    if source != len(base) or target is None or target > _TAG_OBJECT_LIMIT:
        return None
    out = bytearray()
    while at < len(delta) and len(out) <= target:
        op = delta[at]
        at += 1
        if op & 0x80:                                       # copy from the base
            offset = size = 0
            for bit in range(7):
                if op & (1 << bit):
                    if at >= len(delta):
                        return None
                    if bit < 4:
                        offset |= delta[at] << (8 * bit)
                    else:
                        size |= delta[at] << (8 * (bit - 4))
                    at += 1
            size = size or 0x10000
            if offset + size > len(base):
                return None
            out += base[offset:offset + size]
        elif op:                                            # insert what follows
            if at + op > len(delta):
                return None
            out += delta[at:at + op]
            at += op
        else:
            return None
    return bytes(out) if len(out) == target else None


def _pack_entry(pack, index: Path, offset: int, depth: int = 0) -> Optional[bytes]:
    """The tag object a pack holds at `offset` — whole, or as a delta over
    another of its entries (two signed tags share little; two plain ones
    much), followed _DELTA_DEPTH deep. Unchecked: its reader hashes it."""
    if offset < 0 or depth > _DELTA_DEPTH:
        return None
    pack.seek(offset)
    entry = pack.read(_TAG_OBJECT_LIMIT + 4096)
    if not entry:
        return None
    kind = (entry[0] >> 4) & 7
    size, at = _varint(bytes([entry[0] & 0x8F]) + entry[1:], 0)   # the first byte carries four bits of it
    if size is None:
        return None
    size = (size & 0x0F) | ((size >> 7) << 4)
    if size > _TAG_OBJECT_LIMIT:
        return None
    if kind == 4:                                           # a tag, whole
        return zlib.decompressobj().decompress(entry[at:], size)
    if kind == 6:                                           # a delta over the entry `back` bytes before
        if at >= len(entry):
            return None
        byte = entry[at]
        at += 1
        back = byte & 0x7F
        while byte & 0x80:
            if at >= len(entry) or back > 2 ** 56:
                return None
            byte = entry[at]
            at += 1
            back = ((back + 1) << 7) | (byte & 0x7F)
        base_at = offset - back
    elif kind == 7:                                         # a delta over the object named
        base_at = _pack_offset(index, entry[at:at + 20]) if len(entry) >= at + 20 else None
        at += 20
        if base_at is None:
            return None
    else:
        return None
    delta = zlib.decompressobj().decompress(entry[at:], size)
    base = _pack_entry(pack, index, base_at, depth + 1)
    return None if base is None else _delta_applied(base, delta)


def _packed_object(common: Path, oid: str) -> Optional[bytes]:
    """The tag object `oid` from the pack that holds it, unchecked."""
    try:
        names = os.scandir(common / "objects" / "pack")
    except (FileNotFoundError, NotADirectoryError):
        return None
    with names:
        indexes = sorted(itertools.islice(
            (entry.path for entry in names if entry.name.endswith(".idx")), _PACK_INDEXES))
    for index in indexes:
        offset = _pack_offset(Path(index), bytes.fromhex(oid))
        if offset is None:
            continue
        with open(index[:-len(".idx")] + ".pack", "rb") as pack:
            return _pack_entry(pack, Path(index), offset)
    return None


def _tag_object(common: Path, oid: str) -> Optional[bytes]:
    """The tag object `oid`, read from the object store by hand — loose, or
    whole in a pack — and only as what hashes to its name. None for any
    other object or one past _TAG_OBJECT_LIMIT."""
    try:
        with open(common / "objects" / oid[:2] / oid[2:], "rb") as f:
            loose = f.read(_TAG_OBJECT_LIMIT + 4096)
    except (FileNotFoundError, NotADirectoryError):
        body = _packed_object(common, oid)
    else:
        header, _, body = zlib.decompressobj().decompress(loose, _TAG_OBJECT_LIMIT + 32).partition(b"\0")
        if not header.startswith(b"tag "):
            return None
    if body is None or hashlib.sha1(b"tag %d\0" % len(body) + body).hexdigest() != oid:
        return None
    return body


def _head_release(root: Path) -> Optional[str]:
    version = installer_version()
    dirs = _git_dirs(root)
    head = _head_oid(root)
    if version == (0, 0, 0) or dirs is None or head is None:
        return None
    git_dir, common = dirs
    name = "v%d.%d.%d" % version
    for ref in (RELEASE_REFS + name, "refs/tags/" + name):
        oid, peeled = _ref_oid(git_dir, common, ref)
        # packed-refs' peeled line spares the read of a tag that names another commit
        if oid is None or (peeled is not None and peeled != head):
            continue
        raw = _tag_object(common, oid)
        if raw is None:
            continue
        try:
            headers = release_verify.tag_headers(raw)
        except release_verify.VerifyError:
            continue
        if (headers["object"], headers["type"], headers["tag"]) == (head, "commit", name):
            return name
    return None


def head_release(root: Path) -> Optional[str]:
    """The release the checkout under `root` is ON — `v0.2.13` — or None:
    the tag of the version this tree states (a release's tree states its
    own, scripts/release.py refuses any other), where a ref of that name —
    one this updater fetched (RELEASE_REFS) or the tree's own (refs/tags) —
    names an annotated tag that carries that name and names HEAD's commit.
    The tag object itself is read, whether its ref is loose or packed.

    What a node says it runs (its start line, /config), never a judgement
    of a release: no signature is checked here — `survey` judges, with git
    and the signers' list, before a checkout is ever moved. Read from `.git`
    by hand like head_commit, so it answers None where the tag cannot be
    read that way (a repository that is not SHA-1, a folder that is not as
    git writes it — _unreadable): a commit
    past the release, and a tree with no tag of it, say their commit alone.
    Never an error."""
    try:
        return _head_release(root)
    except Exception as exc:
        _unreadable("the release's tag", repr(exc))
        return None


def _update_failed(config: dict, step: str, error: str) -> None:
    """Support diagnostics: a failed update is exactly the moment a node
    goes quiet — recorded before the services come back."""
    from desktop.config_manager import get_data_dir, local_db_dsn
    from desktop.p2p import diag_events
    diag_events.record_or_spool(local_db_dsn(config), get_data_dir(), "update.failed",
                                {"step": step, "error": str(error)[:500]})


def is_git_repo() -> bool:
    """Is OUR tree a checkout — not merely something's work tree.

    `--is-inside-work-tree` answers for the nearest repository ABOVE the
    directory too, and a packaged install lives under $HOME, which plenty of
    people keep in git for their dotfiles. Answering "yes" there would point
    every later git call — `fetch`, and then `reset` — at that repository.
    """
    result = _git_cmd(["rev-parse", "--show-toplevel"])
    if result.returncode != 0:
        return False
    return Path(result.stdout.strip()).resolve() == get_project_root().resolve()


def check_for_updates() -> Optional[Survey]:
    """Fetch the releases and judge them against this checkout; None when
    there is nothing to judge (not a checkout of ours, or the fetch failed —
    logged)."""
    if not is_git_repo():
        return None
    _migrate_origin()
    try:
        return survey(installed_signers(), _rev("HEAD"))
    except UpdateError as exc:
        logger.warning(str(exc))
        return None


def get_update_changelog(old_hash: str) -> List[str]:
    """Commit messages between old hash and current HEAD — a release always
    descends from what it replaced."""
    result = _git_cmd(["log", "--oneline", f"{old_hash}..HEAD"])
    if result.returncode != 0:
        return []
    return [line.strip() for line in result.stdout.strip().split("\n") if line.strip()]


def has_new_migrations(old_hash: str) -> bool:
    """Check if new migration files were added since old_hash."""
    result = _git_cmd(["diff", "--name-only", "--diff-filter=A", old_hash, "HEAD"])
    if result.returncode != 0:
        return False
    added_files = result.stdout.strip().split("\n")
    return any("migrations/" in f and f.endswith(".sql") for f in added_files)


def has_launcher_changes(old_hash: str) -> bool:
    """Whether the pulled range touched code that runs INSIDE the launcher
    process. The backend is a subprocess and picks new code up when it is
    restarted below; desktop/ runs in this process, and Python cannot swap
    the module a live object was built from — the launcher has to come back
    as a new process (LauncherApp._restart_self).

    Deliberately coarse: a migration or a backend-only helper under desktop/
    counts too. An extra restart costs seconds; a missed one leaves the node
    running code the user believes was updated."""
    result = _git_cmd(["diff", "--name-only", old_hash, "HEAD"])
    if result.returncode != 0:
        return False
    changed = result.stdout.strip().split("\n")
    return any(f.startswith("desktop/") for f in changed)


def reset_to_release() -> Tuple[str, Optional[Release], Optional[str], Optional[Path]]:
    """
    Move the checkout to the release a fresh survey offers.

    Returns:
        (old_hash before the move, the release moved to or None, error or
        None, the patch the tree's own edits were set aside in or None)
    """
    old_hash = _rev("HEAD") or ""

    status = _git_cmd(["status", "--porcelain", "--untracked-files=no"])
    if status.returncode != 0:
        return old_hash, None, f"git status failed: {status.stderr.strip()}", None
    # An edited tracked file in a working tree is somebody's work and stops
    # the update, as `git pull` did. In the tree a carrier installed it is
    # nobody's — a hot-patch left from a test, a tool that rewrote a file —
    # and stopping there stranded the node on "Update failed" until someone
    # ran git by hand (2026-09-25); it is set aside below instead.
    edited = bool(status.stdout.strip())
    if edited and not is_managed_install():
        return old_hash, None, "the checkout has local modifications", None

    _migrate_origin()
    # Judged again here, not taken from the check the dialog showed: the move
    # goes to the commit this survey verified, by its id — never to a ref
    # something could repoint in between.
    try:
        release = survey(installed_signers(), old_hash).offered if old_hash else None
    except UpdateError as exc:
        return old_hash, None, str(exc), None
    if release is None:
        return old_hash, None, "no signed release descends from this checkout", None

    set_aside = None
    if edited:
        # Kept, never discarded: `git apply` on the old commit brings the
        # edits back. A file in the data dir, not a stash.
        from datetime import datetime
        from desktop.config_manager import get_data_dir
        folder = get_data_dir() / "local-edits"
        folder.mkdir(exist_ok=True)
        set_aside = folder / f"{datetime.now():%Y%m%d-%H%M%S}-{old_hash[:12]}.patch"
        saved = _git_cmd(["diff", "--binary", f"--output={set_aside}", "HEAD"])
        if saved.returncode != 0:
            return old_hash, None, f"could not set local edits aside: {saved.stderr.strip()}", None
        logger.warning(f"local edits set aside in {set_aside}")

    result = _git_cmd(["reset", "--hard", release.commit], timeout=120)
    if result.returncode != 0:
        return old_hash, None, f"git reset failed: {result.stderr.strip()}", set_aside
    logger.info(f"checkout moved {old_hash[:12]} -> {release.commit[:12]} ({release.name})")
    return old_hash, release, None, set_aside


_STATED_VERSION = re.compile(r'^VERSION = "([^"]*)"$', re.M)


def _stated_version(text: str) -> Tuple[int, int, int]:
    """The version a tree's desktop/build_common.py states — (0, 0, 0) when
    it states none."""
    match = _STATED_VERSION.search(text)
    return (release_verify.parse_version("v" + match.group(1)) if match else None) or (0, 0, 0)


def installer_version() -> Tuple[int, int, int]:
    """The version of the tree this code runs from — for install-release, the
    installer's payload."""
    return _stated_version((Path(__file__).parent / "build_common.py").read_text(encoding="utf-8"))


def install_release(app_dir: str, signers_file: str) -> Optional[Release]:
    """What an installer's bootstrap checks out (desktop/windows/bootstrap.py,
    desktop/macos/bootstrap.py), in `app_dir` — a repository with its origin
    set: the newest release the chain reaches from the installer's own
    release (its stated version) and list that this list also verifies.

    A first install has no HEAD there (files of an earlier attempt or the
    bundled copy may lie in it). A reinstall over a clone does, and it moves
    the clone only when the installer is NEWER than the release the clone
    holds, and only to a release newer still — both judged by the version
    each tree states, which needs no key: an installer as old as the clone,
    or older, knows nothing the clone's own list does not, and its list must
    never undo a rotation the clone took. This is how a reinstall reaches a
    release that rewrote history, or one signed by keys only a new installer
    lists. The release moved to, or None when there is none to take
    (nothing verifies, nothing newer); UpdateError when origin cannot be read
    — a reinstall that could not look is tried again, never recorded as
    having found nothing."""
    signers = release_verify.parse_allowed_signers(
        Path(signers_file).read_text(encoding="utf-8"), signers_file)
    head = _rev("HEAD", app_dir)
    mine = installer_version()
    if head:
        held = _stated_version(_git_cmd(["show", "HEAD:desktop/build_common.py"], app_dir).stdout)
        if mine <= held:
            logger.info(f"this installer ({'.'.join(map(str, mine))}) is not newer than the tree "
                        f"({'.'.join(map(str, held))}) — the tree is left to its own updater")
            return None
    release = survey(signers, None, app_dir, min_version=mine).offered
    if release is None:
        logger.warning("origin holds no release that verifies against the installer's signers")
        return None
    if head and release.version <= held:
        logger.info(f"no release newer than the tree's ({'.'.join(map(str, held))}) verifies here")
        return None
    result = _git_cmd(["checkout", "--force", "-B", "main", release.commit], app_dir, timeout=300)
    if result.returncode != 0:
        raise UpdateError(f"git checkout of {release.name} failed: {result.stderr.strip()}")
    logger.info(f"installed {release.name} ({release.commit[:12]})")
    return release


def perform_update(
    service_manager,
    config: dict,
    progress_cb: Optional[Callable] = None,
) -> Tuple[bool, List[str], bool, bool]:
    """
    Full update sequence, with P2P already stopped by the caller (it owns the
    manager, and the slot it empties is what the restart's "up" refills):
    1. Stop backend + tracker (keep PostgreSQL)
    2. Move the checkout to the offered release (fetch, verify, reset --hard
       to its commit; a managed tree's own edits are set aside as a patch
       first)
    3. Run migrations if new ones exist
    4. Restart backend + tracker — UNLESS the launcher itself has to come
       back as a new process, which starts them on its own

    Dependencies are not installed here: each interpreter's owner does that
    from the requirements hash it keeps. The backend start installs
    backend/requirements.txt into the backend's interpreter
    (service_manager._ensure_backend_deps — on Windows not the one this code
    runs on), and a packaged install's bootstrap installs
    desktop/requirements.txt on the relaunch a desktop/ change always
    triggers. The step that used to sit here pip-installed the backend file
    into sys.executable with a five-minute cap: the wrong interpreter on
    Windows, a timeout on every torch bump anywhere.

    Returns:
        (success, changelog_lines, relaunch_launcher, services_up) —
        services_up says whether the backend answered after the restart;
        always False with a relaunch, whose successor starts them.
    """
    service_manager.stop_tracker()
    service_manager.stop_backend()

    if progress_cb:
        progress_cb("Downloading updates...")

    # The services are down from here on: whatever breaks before they are
    # started again, they come back up and the failure is reported — never a
    # node left stopped.
    try:
        old_hash, release, error, set_aside = reset_to_release()
        if not error:
            changelog = [release.label] + get_update_changelog(old_hash)
            migrations = has_new_migrations(old_hash)
            relaunch = has_launcher_changes(old_hash) or set_aside is not None
    except Exception as exc:
        logger.exception("update failed")
        error = f"{type(exc).__name__}: {exc}"
    if error:
        logger.error(f"update failed: {error}")
        _update_failed(config, "checkout", error)
        services_up = (service_manager.start_backend(progress_cb)
                       and service_manager.start_tracker(progress_cb))
        return False, [], False, services_up

    if set_aside:
        changelog.insert(0, f"Local edits to the app were set aside in {set_aside}")

    if migrations:
        if progress_cb:
            progress_cb("Running database migrations...")
        try:
            from desktop.db_init import run_migrations
            password = config.get("postgres_password", "changeme")
            port = config.get("ports", {}).get("postgres", 5432)
            run_migrations(password, port=port, progress_cb=progress_cb)
        except Exception as e:
            logger.error(f"Migration after update failed: {e}")
            _update_failed(config, "migration", str(e))

    # An edit just set aside may still be loaded in this process — a
    # hot-patched desktop/ module — and only a new process drops it (relaunch,
    # above).
    if relaunch:
        # The launcher is about to be replaced by a successor process that
        # starts the services itself — bringing them up here would only be
        # to stop them again a second later.
        if progress_cb:
            progress_cb("Update complete — restarting Sautium...")
        return True, changelog, True, False

    if progress_cb:
        progress_cb("Restarting services...")

    services_up = (service_manager.start_backend(progress_cb)
                   and service_manager.start_tracker(progress_cb))

    if progress_cb and services_up:
        progress_cb("Update complete!")

    return True, changelog, False, services_up


if __name__ == "__main__":
    # The bootstraps' first clone (desktop/windows/bootstrap.py,
    # desktop/macos/bootstrap.py), run from the installer's payload:
    #   python -B -m desktop.updater install-release <app dir> <signers file>
    # Exit 0 with the release checked out, 3 with none to take, 1 when origin
    # could not be read or the checkout failed.
    logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stdout)
    if len(sys.argv) != 4 or sys.argv[1] != "install-release":
        sys.exit("usage: python -m desktop.updater install-release <app dir> <signers file>")
    try:
        sys.exit(0 if install_release(sys.argv[2], sys.argv[3]) else 3)
    except UpdateError as exc:
        logger.error(str(exc))
        sys.exit(1)
