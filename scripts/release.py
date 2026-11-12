#!/usr/bin/env python3
"""
The release steps a maintainer runs in their own terminal (docs/RELEASING.md).

    python scripts/release.py selftest [--repo <checkout>]
    python scripts/release.py tag <version> [--commit <rev>] [--list-change] [--repo <checkout>]
    python scripts/release.py verify <version> [--commit <rev>] [--repo <checkout>]

The checkout is the one this script is in unless --repo names another.

`selftest` checks the one-time setup without touching any tag: it signs a
scratch file the way `git tag -s` would (gpg.ssh.program, user.signingkey,
under the throwaway namespace `sautium-test`) and verifies it with the
launcher's verifier against the keys of the checkout's desktop/release_signers
— a security key must answer with a touch AND its PIN.

`tag` makes the signed release tag `v<version>` and never pushes it:
  1. fetches origin's main and tags;
  2. the commit is this checkout's HEAD unless --commit names another, and
     must be on origin/main — pushed. Never origin's tip by default: that is
     exactly what a push nobody intended controls;
  3. prints what the release carries since the last one — the commits, and
     the key list if it changed, which then needs --list-change: the list a
     release carries decides who signs the next one;
  4. the version must be new and above the last signed release, and the
     commit's desktop/build_common.py VERSION must say it;
  5. the commit's desktop/release_signers must parse whole, with a key
     allowed for `git` — it is the list the NEXT release is verified against;
  6. the commit must descend from the last signed release — launchers refuse
     anything else as a rewritten history;
  7. `git tag -s` with this checkout's own git configuration (gpg.format=ssh,
     user.signingkey, gpg.ssh.program — whichever ssh-keygen drives the key;
     the key's touch, PIN or passphrase are asked by that program, here);
  8. verifies the result with the launcher's own verifier against the list of
     the last signed release (and with `git verify-tag` when
     gpg.ssh.allowedSignersFile is set), deleting the tag if either refuses;
  9. prints the push command.

`verify` says whether `v<version>` on origin is in the release chain and,
with --commit, that it names that commit (scripts/release-publish.sh runs it
before it creates the GitHub release).

The chain: each release is signed by a key on the list of the release
before it and descends from it. Its root is the floor — the first signed
release. `tag` records the floor it makes in this checkout's git
configuration (`sautium.releaseFloor`, the tag object's id), and from then on
the chain starts there and nowhere else: what origin's history says can be
reshaped by a push (a merge whose first parent is a history of the pusher's
own), what this checkout recorded cannot. A checkout without the record (an
auditor's clone) derives the root from the first key list on origin/main's
first-parent line and says so. A tag off the chain — unsigned, signed by
another key, off its history — is no release of this script's.
"""

import argparse
import re
import subprocess
import sys
from pathlib import Path
from typing import List, Optional, Tuple

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
REPO = ROOT

from desktop import release_verify, updater  # noqa: E402

FLOOR_CONFIG = "sautium.releaseFloor"


class Refused(SystemExit):
    def __init__(self, message: str):
        super().__init__(f"release: {message}")


def git(*args, check: bool = True) -> str:
    result = subprocess.run(["git", "-C", str(REPO), *args], capture_output=True, text=True,
                            encoding="utf-8", errors="replace")
    if check and result.returncode != 0:
        raise Refused(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout.strip()


def git_bytes(*args) -> bytes:
    result = subprocess.run(["git", "-C", str(REPO), *args], capture_output=True)
    if result.returncode != 0:
        raise Refused(f"git {' '.join(args)} failed: {result.stderr.decode(errors='replace').strip()}")
    return result.stdout


def is_ancestor(commit: str, of: str) -> bool:
    return subprocess.run(["git", "-C", str(REPO), "merge-base", "--is-ancestor", commit, of]).returncode == 0


def list_text(commit: str) -> Optional[bytes]:
    result = subprocess.run(["git", "-C", str(REPO), "show", f"{commit}:{updater.SIGNERS_PATH}"],
                            capture_output=True)
    return result.stdout if result.returncode == 0 else None


def signers_at(commit: str) -> Tuple[List[release_verify.Signer], int]:
    """The list in `commit`'s tree, and how many key lines it has: a line
    that did not parse is in the count and not in the list."""
    raw = list_text(commit)
    if raw is None:
        return [], 0
    text = raw.decode("utf-8", "replace")
    lines = [line for line in text.splitlines() if line.strip() and not line.strip().startswith("#")]
    return release_verify.parse_allowed_signers(text, f"{commit[:12]}:{updater.SIGNERS_PATH}"), len(lines)


def root_list() -> Tuple[Optional[str], List[release_verify.Signer]]:
    """(the commit, its keys): the first key list main's first-parent line
    carried."""
    for commit in git("log", "--reverse", "--first-parent", "--format=%H", "origin/main", "--",
                      updater.SIGNERS_PATH).split():
        listed, _ = signers_at(commit)
        if listed:
            return commit, listed
    return None, []


def trusted_chain() -> List[Tuple[updater.Release, List[release_verify.Signer]]]:
    """The signed releases, oldest first, each with the list its tree carries
    (which signs the next one)."""
    floor = git("config", "--get", FLOOR_CONFIG, check=False)
    if not floor:
        _, held = root_list()
        print("  (no floor recorded in this checkout: the chain's root is derived from origin/main)")
    listing = git("for-each-ref", "--format=%(refname) %(objecttype)", updater.RELEASE_REFS)
    tags = []
    for line in listing.splitlines():
        ref, kind = line.split()
        name = ref[len(updater.RELEASE_REFS):]
        version = release_verify.parse_version(name)
        if version and version > updater.FLOOR and kind == "tag":
            tags.append((version, name, ref))
    chain = []
    for version, name, ref in sorted(tags):
        if floor and not chain:
            # The floor this checkout made and verified: taken as recorded.
            if git("rev-parse", ref) == floor:
                commit = git("rev-parse", f"{ref}^{{commit}}")
                held = signers_at(commit)[0]
                chain.append((updater.Release(name, version, commit), held))
            continue
        try:
            signed = release_verify.verify_tag(git_bytes("cat-file", "tag", ref), name, held)
        except release_verify.VerifyError as exc:
            print(f"  {name}: not in the release chain ({exc})")
            continue
        if chain and not is_ancestor(chain[-1][0].commit, signed.commit):
            print(f"  {name}: does not descend from {chain[-1][0].name} — not in the release chain")
            continue
        held = signers_at(signed.commit)[0]
        chain.append((updater.Release(name, version, signed.commit), held))
    if floor and not chain:
        raise Refused(f"the floor this checkout recorded ({FLOOR_CONFIG} = {floor[:12]}) is not on origin")
    return chain


def fetch() -> None:
    """origin/main and every tag of the release form, forced, into the
    namespace launchers use (refs/tags left alone); names of another form
    never reach the fetch, where one this filesystem cannot store would fail
    it whole."""
    names = [name for _, name, _ in updater._remote_releases(str(REPO))]
    refspecs = "".join(f"+refs/tags/{n}:{updater.RELEASE_REFS}{n}\n" for n in names)
    result = subprocess.run(["git", "-C", str(REPO), "fetch", "--quiet", "--no-tags", "--stdin", "origin",
                             "+refs/heads/main:refs/remotes/origin/main"],
                            input=refspecs, capture_output=True, text=True)
    if result.returncode != 0:
        raise Refused(f"git fetch failed: {result.stderr.strip()}")
    for ref in git("for-each-ref", "--format=%(refname)", updater.RELEASE_REFS).split():
        if ref[len(updater.RELEASE_REFS):] not in names:
            git("update-ref", "-d", ref)


def release_name(version: str) -> Tuple[str, Tuple[int, int, int]]:
    name = version if version.startswith("v") else f"v{version}"
    parsed = release_verify.parse_version(name)
    if parsed is None:
        raise Refused(f"{version!r} is not <major>.<minor>.<patch>")
    return name, parsed


def describe(signers: List[release_verify.Signer]) -> List[str]:
    import base64
    import hashlib
    return [f"    {','.join(s.principals)}  {s.key_type}  SHA256:"
            f"{base64.b64encode(hashlib.sha256(s.key_blob).digest()).decode().rstrip('=')}"
            f"  namespaces={','.join(s.namespaces) if s.namespaces else '(any)'}" for s in signers]


def tag(version: str, rev: Optional[str], list_change: bool) -> None:
    name, parsed = release_name(version)
    fetch()
    commit = git("rev-parse", "--verify", f"{rev or 'HEAD'}^{{commit}}")
    if not is_ancestor(commit, "origin/main"):
        raise Refused(f"{commit[:12]} is not on origin/main — push it first")
    if f"refs/tags/{name}" in [line.split("\t")[-1] for line in
                                git("ls-remote", "--refs", "origin", f"refs/tags/{name}").splitlines()]:
        raise Refused(f"origin already has a tag {name}")

    chain = trusted_chain()
    root_commit, root = root_list()
    if chain:
        previous, held = chain[-1]
        if parsed <= previous.version:
            raise Refused(f"{name} is not above {previous.name}, the last signed release")
        if not is_ancestor(previous.commit, commit):
            raise Refused(f"{commit[:12]} does not descend from {previous.name} — every launcher would refuse it")
        since, before = previous.commit, list_text(previous.commit)
        print(f"Last signed release: {previous.name} ({previous.commit[:12]}).")
    else:
        if parsed <= updater.FLOOR:
            raise Refused(f"{name} is not above v0.1.1, the unsigned floor")
        if root_commit is None:
            raise Refused(f"origin/main has never carried a key in {updater.SIGNERS_PATH}")
        held, since, before = root, None, list_text(root_commit)
        print(f"No signed release yet: {name} is the floor, signed for the key list main first carried "
              f"({root_commit[:12]}).")

    stated = re.search(r'^VERSION = "([^"]*)"$', git("show", f"{commit}:desktop/build_common.py"), re.M)
    if not stated or stated.group(1) != name[1:]:
        raise Refused(f"desktop/build_common.py at {commit[:12]} says VERSION = "
                      f"{stated.group(1) if stated else '?'!r}, not {name[1:]!r} — commit the bump first")
    own, lines = signers_at(commit)
    if not own or len(own) != lines or not any(s.allows(release_verify.GIT_NAMESPACE) for s in own):
        raise Refused(f"{updater.SIGNERS_PATH} at {commit[:12]} has {lines} key line(s), {len(own)} of them "
                      f"valid, {sum(s.allows(release_verify.GIT_NAMESPACE) for s in own)} for `git` — "
                      "the next release could not be verified")

    log = git("log", "--oneline", "--no-decorate", f"{since}..{commit}" if since else commit)
    entries = log.splitlines()
    print(f"\n{name} on {commit[:12]} carries {len(entries)} commit(s){' since ' + chain[-1][0].name if chain else ''}:")
    for line in entries[:30]:
        print(f"    {line}")
    if len(entries) > 30:
        print(f"    … and {len(entries) - 30} more")
    if list_text(commit) != before:
        print(f"\nThe key list changes in this release — these keys sign the NEXT one:")
        print("\n".join(describe(own)))
        if not list_change:
            raise Refused("the release changes desktop/release_signers: check the keys above, "
                          "then run again with --list-change")
    print(f"\nThe keys that may sign {name}:")
    print("\n".join(describe(held)))

    if git("config", "--get", "gpg.format", check=False) != "ssh":
        raise Refused("gpg.format is not ssh in this checkout — see docs/RELEASING.md, one-time setup")
    print(f"\nSigning {name} on {commit[:12]} — the key may ask for a touch and its PIN.", flush=True)
    signed = subprocess.run(["git", "-C", str(REPO), "tag", "-s", "-m", f"Sautium {name[1:]}", name, commit])
    if signed.returncode != 0:
        raise Refused("git tag -s failed")

    problem = None
    try:
        made = release_verify.verify_tag(git_bytes("cat-file", "tag", f"refs/tags/{name}"), name, held)
        if made.commit != commit:
            problem = f"the tag names {made.commit[:12]}, not {commit[:12]}"
    except release_verify.VerifyError as exc:
        problem = f"the launcher's verifier refuses it: {exc}"
    if problem is None and git("config", "--get", "gpg.ssh.allowedSignersFile", check=False):
        if subprocess.run(["git", "-C", str(REPO), "verify-tag", name]).returncode != 0:
            problem = "git verify-tag refuses it"
    if problem:
        git("tag", "-d", name)
        raise Refused(f"{name} deleted again: {problem}")

    if not chain:
        git("config", FLOOR_CONFIG, git("rev-parse", f"refs/tags/{name}"))
        print(f"\nThe floor is recorded in this checkout ({FLOOR_CONFIG}): the chain starts at {name}.")
    print(f"\n{name} is signed by {', '.join(made.signer.principals)} and verifies as launchers verify it.")
    print("Push it:\n")
    print(f"    git -C {REPO} push origin refs/tags/{name}\n")


def selftest() -> None:
    import tempfile
    program = git("config", "--get", "gpg.ssh.program", check=False) or "ssh-keygen"
    key = git("config", "--get", "user.signingkey", check=False)
    if git("config", "--get", "gpg.format", check=False) != "ssh" or not key:
        raise Refused("gpg.format=ssh and user.signingkey are not both set in this checkout")
    listed = signers_at("HEAD")[0]
    # The list names the `git` namespace; the self-test signs under another,
    # so nothing it makes could ever pass for a release.
    keys = [release_verify.Signer(s.principals, None, s.key_type, s.key_blob) for s in listed]
    with tempfile.TemporaryDirectory() as scratch:
        message = Path(scratch) / "selftest"
        message.write_bytes(b"sautium release selftest\n")
        print(f"Signing a scratch file with {program} — the key may ask for a touch and its PIN.", flush=True)
        if subprocess.run([program, "-Y", "sign", "-n", "sautium-test", "-f", key, str(message)]).returncode != 0:
            raise Refused(f"{program} -Y sign failed")
        armored = Path(f"{message}.sig").read_bytes()
        try:
            signer = release_verify.verify(message.read_bytes(), armored, keys, "sautium-test")
        except release_verify.VerifyError as exc:
            raise Refused(f"the launcher's verifier refuses the signature: {exc}")
    print(f"OK: signed by {', '.join(signer.principals)} ({signer.key_type}) as launchers require.")


def verify(version: str, rev: Optional[str]) -> None:
    name, _ = release_name(version)
    fetch()
    found = next((release for release, _ in trusted_chain() if release.name == name), None)
    if found is None:
        raise Refused(f"{name} is not in the release chain")
    if rev and git("rev-parse", "--verify", f"{rev}^{{commit}}") != found.commit:
        raise Refused(f"{name} names {found.commit[:12]}, not {rev}")
    print(f"{name} is a signed release in the chain: {found.commit}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Sign and check Sautium release tags.")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("selftest").add_argument("--repo", type=Path, default=ROOT)
    for command in ("tag", "verify"):
        p = sub.add_parser(command)
        p.add_argument("version")
        p.add_argument("--commit")
        p.add_argument("--repo", type=Path, default=ROOT)
        if command == "tag":
            p.add_argument("--list-change", action="store_true",
                           help="the release changes desktop/release_signers, and that is intended")
    args = parser.parse_args()
    global REPO
    REPO = args.repo.resolve()
    if args.command == "selftest":
        selftest()
    elif args.command == "tag":
        tag(args.version, args.commit, args.list_change)
    else:
        verify(args.version, args.commit)


if __name__ == "__main__":
    main()
