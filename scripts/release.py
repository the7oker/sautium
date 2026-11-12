#!/usr/bin/env python3
"""
The release steps a maintainer runs in their own terminal (docs/RELEASING.md).

    python scripts/release.py selftest [--repo <checkout>]
    python scripts/release.py tag <version> [--commit <rev>] [--repo <checkout>]
    python scripts/release.py verify <version> [--commit <rev>] [--repo <checkout>]

The checkout is the one this script is in unless --repo names another.

`selftest` checks the one-time setup without touching any tag: it signs a
scratch file the way `git tag -s` would (gpg.ssh.program, user.signingkey,
under the throwaway namespace `sautium-test`) and verifies it with the
launcher's verifier against the keys of the checkout's desktop/release_signers
— a security key must answer with a touch AND its PIN.

`tag` makes the signed release tag `v<version>` and never pushes it:
  1. fetches origin's main and tags;
  2. the commit (origin/main's tip unless --commit) must be on origin/main —
     pushed;
  3. the version must be new and above every `v*` tag origin has, and the
     commit's desktop/build_common.py VERSION must say it;
  4. the commit's desktop/release_signers must parse whole, one key or more —
     it is the list the NEXT release is verified against;
  5. the commit must descend from the last trusted release — launchers refuse
     anything else as a rewritten history;
  6. `git tag -s` with this checkout's own git configuration (gpg.format=ssh,
     user.signingkey, gpg.ssh.program — whichever ssh-keygen drives the key;
     the key's touch, PIN or passphrase are asked by that program, here);
  7. verifies the result with the launcher's own verifier against the list
     installed launchers hold (and with `git verify-tag` when
     gpg.ssh.allowedSignersFile is set), deleting the tag if either refuses;
  8. prints the push command.

`verify` says whether `v<version>` on origin is a release launchers take —
signed by a key on the list of the release before it — and, with --commit,
that it names that commit (scripts/release-publish.sh runs it before it
creates the GitHub release).

"The list launchers hold" is followed from the first signed release: each
release's tree governs the next one, as desktop/updater.py applies it on every
node. The first signed release (the floor) is verified against its own tree's
list — the list the launchers that took this code from main carry.
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


class Refused(SystemExit):
    def __init__(self, message: str):
        super().__init__(f"release: {message}")


def git(*args, check: bool = True) -> str:
    result = subprocess.run(["git", "-C", str(REPO), *args], capture_output=True, text=True)
    if check and result.returncode != 0:
        raise Refused(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout.strip()


def git_bytes(*args) -> bytes:
    result = subprocess.run(["git", "-C", str(REPO), *args], capture_output=True)
    if result.returncode != 0:
        raise Refused(f"git {' '.join(args)} failed: {result.stderr.decode(errors='replace').strip()}")
    return result.stdout


def signers_at(commit: str) -> Tuple[List[release_verify.Signer], int]:
    """The list in `commit`'s tree, and how many key lines it has: a line
    that did not parse is in the count and not in the list."""
    result = subprocess.run(["git", "-C", str(REPO), "show", f"{commit}:{updater.SIGNERS_PATH}"],
                            capture_output=True)
    if result.returncode != 0:
        return [], 0
    text = result.stdout.decode("utf-8", "replace")
    lines = [line for line in text.splitlines() if line.strip() and not line.strip().startswith("#")]
    return release_verify.parse_allowed_signers(text, f"{commit[:12]}:{updater.SIGNERS_PATH}"), len(lines)


def remote_versions() -> List[str]:
    """Every `v<semver>` tag origin has, signed or not."""
    names = []
    for line in git("ls-remote", "--tags", "--refs", "origin").splitlines():
        name = line.split("refs/tags/", 1)[-1]
        if release_verify.parse_version(name):
            names.append(name)
    return names


def trusted_chain() -> List[Tuple[updater.Release, List[release_verify.Signer]]]:
    """The releases launchers take, oldest first, each with the list its tree
    carries (which governs the next): the fetched tags walked from the oldest
    up, as nodes take them one by one."""
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
        raw = git_bytes("cat-file", "tag", ref)
        if chain:
            held = chain[-1][1]
        else:
            held, _ = signers_at(release_verify.tag_headers(release_verify.split_tag(raw)[0])["object"])
        try:
            signed = release_verify.verify_tag(raw, name, held)
        except release_verify.VerifyError as exc:
            print(f"  {name}: not a release launchers take ({exc})")
            continue
        if chain and not is_ancestor(chain[-1][0].commit, signed.commit):
            print(f"  {name}: does not descend from {chain[-1][0].name} — launchers refuse it")
            continue
        chain.append((updater.Release(name, version, signed.commit), signers_at(signed.commit)[0]))
    return chain


def is_ancestor(commit: str, of: str) -> bool:
    return subprocess.run(["git", "-C", str(REPO), "merge-base", "--is-ancestor", commit, of]).returncode == 0


def fetch() -> None:
    git("fetch", "--quiet", "--no-tags", "origin", "+refs/heads/main:refs/remotes/origin/main")
    error = updater.fetch_releases(str(REPO))
    if error:
        raise Refused(error)


def release_name(version: str) -> Tuple[str, Tuple[int, int, int]]:
    name = version if version.startswith("v") else f"v{version}"
    parsed = release_verify.parse_version(name)
    if parsed is None:
        raise Refused(f"{version!r} is not <major>.<minor>.<patch>")
    return name, parsed


def tag(version: str, rev: Optional[str]) -> None:
    name, parsed = release_name(version)
    fetch()
    commit = git("rev-parse", "--verify", f"{rev or 'origin/main'}^{{commit}}")
    if not is_ancestor(commit, "origin/main"):
        raise Refused(f"{commit[:12]} is not on origin/main — push it first")

    existing = remote_versions()
    if name in existing:
        raise Refused(f"origin already has a tag {name}")
    newest = max([release_verify.parse_version(t) for t in existing] + [updater.FLOOR])
    if parsed <= newest:
        raise Refused(f"{name} is not above v{'.'.join(map(str, newest))}, origin's newest tag")
    stated = re.search(r'^VERSION = "([^"]*)"$', git("show", f"{commit}:desktop/build_common.py"), re.M)
    if not stated or stated.group(1) != name[1:]:
        raise Refused(f"desktop/build_common.py at {commit[:12]} says VERSION = "
                      f"{stated.group(1) if stated else '?'!r}, not {name[1:]!r} — commit the bump first")

    own, lines = signers_at(commit)
    if not own or len(own) != lines:
        raise Refused(f"{updater.SIGNERS_PATH} at {commit[:12]} has {lines} key line(s), "
                      f"{len(own)} of them valid — the next release could not be verified")

    chain = trusted_chain()
    if not chain:
        held = own
        print(f"No signed release yet: {name} is the floor, verified against {commit[:12]}'s own list.")
    else:
        previous, held = chain[-1]
        print(f"Last release launchers take: {previous.name} ({previous.commit[:12]}).")
        if not is_ancestor(previous.commit, commit):
            raise Refused(f"{commit[:12]} does not descend from {previous.name} — every launcher would refuse it")

    if git("config", "--get", "gpg.format", check=False) != "ssh":
        raise Refused("gpg.format is not ssh in this checkout — see docs/RELEASING.md, one-time setup")
    print(f"Signing {name} on {commit[:12]} — the key may ask for a touch and its PIN.")
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
        print(f"Signing a scratch file with {program} — the key may ask for a touch and its PIN.")
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
        raise Refused(f"{name} is not a release launchers take")
    if rev and git("rev-parse", "--verify", f"{rev}^{{commit}}") != found.commit:
        raise Refused(f"{name} names {found.commit[:12]}, not {rev}")
    print(f"{name} is a signed release launchers take: {found.commit}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Sign and check Sautium release tags.")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("selftest").add_argument("--repo", type=Path, default=ROOT)
    for command in ("tag", "verify"):
        p = sub.add_parser(command)
        p.add_argument("version")
        p.add_argument("--commit")
        p.add_argument("--repo", type=Path, default=ROOT)
    args = parser.parse_args()
    global REPO
    REPO = args.repo.resolve()
    if args.command == "selftest":
        selftest()
    else:
        (tag if args.command == "tag" else verify)(args.version, args.commit)


if __name__ == "__main__":
    main()
