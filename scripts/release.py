#!/usr/bin/env python3
"""
The release steps a maintainer runs in their own terminal (docs/RELEASING.md).

    python scripts/release.py selftest [--repo <checkout>]
    python scripts/release.py tag <version> [--commit <rev>] [--list-change | --new-root] [--repo <checkout>]
    python scripts/release.py verify <version> [--commit <rev>] [--repo <checkout>]
    python scripts/release.py sign-file <file> [--repo <checkout>]

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
     anything else as a rewritten history — and the chain must not fork;
  7. the key user.signingkey names must be on the list of the last signed
     release AND on the commit's own list: a release whose own list drops its
     signer leaves every installer built from it unable to verify anything
     (checked before the signature, so no touch is spent on a refusal);
  8. `git tag -s` with this checkout's own git configuration (gpg.format=ssh,
     user.signingkey, gpg.ssh.program — whichever ssh-keygen drives the key;
     the key's touch, PIN or passphrase are asked by that program, here);
  9. verifies the result with the launcher's own verifier against the list of
     the last signed release and against its own (and with `git verify-tag`
     when gpg.ssh.allowedSignersFile is set), deleting the tag if any refuses;
 10. prints the push command.

`tag --new-root` is the recovery when every key of the list is lost: the
release is signed by a key of its own new list, need not descend from the
last release, and becomes this checkout's floor after a confirmation typed
here. Installed launchers stop at the old chain — none trusts the new key —
and a reinstall from an installer built at the new root moves on. It prints
the public notice for the release notes.

`sign-file` signs a file (the installers' SHA256SUMS.txt — release-publish.sh
runs it) under the `sautium-file` namespace with the same key setup, and
verifies the signature with the launcher's verifier against the checkout's
list.

`verify` says whether `v<version>` on origin is in the release chain and,
with --commit, that it names that commit (scripts/release-publish.sh runs it
before it creates the GitHub release).

The chain is the one launchers walk, by the same code (updater.walk_chain):
each release is signed by a key on the list of the release before it and
descends from it; two signed continuations of one release are a fork, and
nothing above it is in the chain. Its root is the floor — the first signed
release, or the last new root. `tag` records the floor it makes in this checkout's git
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
# The namespace the installers' SHA256SUMS.txt is signed under: a key the
# list allows for it signs files; only a key allowed for `git` signs releases.
FILE_NAMESPACE = "sautium-file"


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


def trusted_chain() -> updater.Chain:
    """The release chain from the floor, walked as launchers walk it. Its
    releases, oldest first, are `in_chain(chain)`."""
    floor = git("config", "--get", FLOOR_CONFIG, check=False)
    listing = git("for-each-ref", "--format=%(refname) %(objecttype) %(objectname)", updater.RELEASE_REFS)
    tags = []
    for line in listing.splitlines():
        ref, kind, oid = line.split()
        name = ref[len(updater.RELEASE_REFS):]
        version = release_verify.parse_version(name)
        if version and version > updater.FLOOR and kind == "tag":
            tags.append((version, name, ref, oid))
    tags.sort()
    if floor:
        # The floor this checkout made and verified: taken as recorded.
        recorded = next((tag for tag in tags if tag[3] == floor), None)
        if recorded is None:
            raise Refused(f"the floor this checkout recorded ({FLOOR_CONFIG} = {floor[:12]}) is not on origin — "
                          f"push it, or if it was never pushed and is to be made again, "
                          f"`git config --unset {FLOOR_CONFIG}` first")
        version, name, ref, _ = recorded
        commit = git("rev-parse", f"{ref}^{{commit}}")
        start = updater.Link(updater.Release(name, version, commit), commit,
                             tuple(updater.signers_at(commit, str(REPO))))
        tags = [tag for tag in tags if tag[0] > version]
    else:
        print("  (no floor recorded in this checkout: the chain's root is derived from origin/main)")
        start = updater.Link(None, None, tuple(root_list()[1]))
    objects = updater._objects([ref for _, _, ref, _ in tags], [], str(REPO)) if tags else {}
    candidates = []
    for version, name, ref, oid in tags:
        _, kind, raw = objects[ref]
        why = updater.admitted(kind, raw)
        if why:
            print(f"  {name}: not in the release chain ({why})")
        else:
            candidates.append((version, name, oid, raw))
    chain = updater.walk_chain(start, candidates, str(REPO))
    for _, name, why in chain.refused:
        print(f"  {name}: not in the release chain ({why})")
    for release in chain.rewrites:
        print(f"  {release.name}: does not descend from {chain.links[0].label} — not in the release chain")
    for _, name, _, _, why in chain.forks:
        print(f"  {name}: a fork — {why}; not in the release chain")
    return chain


def in_chain(chain: updater.Chain) -> List[Tuple[updater.Release, List[release_verify.Signer]]]:
    """The releases a launcher at the floor takes, oldest first, each with
    the list its tree carries (which signs the next one)."""
    links = ([chain.links[0]] if chain.links[0].release else []) + chain.reach()
    return [(link.release, list(link.signers)) for link in links]


def fingerprint(blob: bytes) -> str:
    import base64
    import hashlib
    return "SHA256:" + base64.b64encode(hashlib.sha256(blob).digest()).decode().rstrip("=")


def signing_key() -> Tuple[str, bytes]:
    """(type, public blob) of the key `git tag -s` signs with: user.signingkey
    as a `key::` literal, a public key file, or a private key file (its .pub
    beside it, else `ssh-keygen -y` through gpg.ssh.program)."""
    import base64
    import binascii
    value = git("config", "--get", "user.signingkey", check=False)
    if not value:
        raise Refused("user.signingkey is not set in this checkout — see docs/RELEASING.md, one-time setup")
    if value.startswith("key::"):
        text = value[len("key::"):]
    else:
        path = Path(value).expanduser()
        public = path if path.suffix == ".pub" else Path(f"{path}.pub")
        if public.exists():
            text = public.read_text(encoding="utf-8", errors="replace")
        else:
            program = git("config", "--get", "gpg.ssh.program", check=False) or "ssh-keygen"
            derived = subprocess.run([program, "-y", "-f", str(path)], capture_output=True, text=True)
            if derived.returncode != 0:
                raise Refused(f"cannot read the public key of {path}: {derived.stderr.strip()}")
            text = derived.stdout
    fields = text.split()
    try:
        return fields[0], base64.b64decode(fields[1], validate=True)
    except (IndexError, binascii.Error):
        raise Refused(f"user.signingkey ({value}) names no public key this script can read") from None


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
    return [f"    {','.join(s.principals)}  {s.key_type}  {fingerprint(s.key_blob)}"
            f"  namespaces={','.join(s.namespaces) if s.namespaces else '(any)'}" for s in signers]


def listed(blob: bytes, signers: List[release_verify.Signer], namespace: str) -> bool:
    return any(s.key_blob == blob and s.allows(namespace) for s in signers)


def new_root_words(name: str, last: updater.Release, signers: List[release_verify.Signer]) -> str:
    """What --new-root asks the maintainer to confirm (a draft for the
    owner's words review)."""
    return "\n".join([
        f"{name} is a NEW ROOT: it starts a new release chain, signed for its own key list, not for the",
        f"list of {last.name}, the old chain's last release. What that does:",
        f"  - every installed Sautium stays on the release it has: no key it trusts signed {name}, so",
        f"    it is offered nothing more, ever, and its update check names {name} as untrusted;",
        f"  - a person moves on only by installing Sautium again, from an installer built at {name} or",
        "    later — whose payload carries the new list; their library and settings stay;",
        f"  - this checkout's floor moves to {name}: from now on this script verifies the new chain",
        "    alone, and every release after it is signed by one of these keys:",
        *describe(signers),
    ])


def new_root_notice(name: str, last: updater.Release, oid: str, signers: List[release_verify.Signer]) -> str:
    """The public notice for the new root's release notes (a draft for the
    owner's words review)."""
    keys = "\n".join(f"- `{','.join(s.principals)}`: {s.key_type} `{fingerprint(s.key_blob)}`" for s in signers)
    return f"""**Sautium {name[1:]} starts a new chain of signed releases.** The keys that signed every release up to
{last.name} can no longer sign. An installed Sautium checks every update against the keys it already trusts, so it
will not update past {last.name} on its own: that is the protection working, not a fault.

To move on, install Sautium again from this release's installer (or https://sautium.net/download), over the one you
have. Your library, settings and listening history stay.

The new release keys (also in `desktop/release_signers` at {name}):
{keys}

The new chain's first release, {name}, is the tag object `{oid}`. To check releases from it on yourself:
`git config sautium.releaseFloor {oid}` in a clone, then `python scripts/release.py verify <version>`."""


def tag(version: str, rev: Optional[str], list_change: bool, new_root: bool = False) -> None:
    name, parsed = release_name(version)
    fetch()
    commit = git("rev-parse", "--verify", f"{rev or 'HEAD'}^{{commit}}")
    if not is_ancestor(commit, "origin/main"):
        raise Refused(f"{commit[:12]} is not on origin/main — push it first")
    if f"refs/tags/{name}" in [line.split("\t")[-1] for line in
                                git("ls-remote", "--refs", "origin", f"refs/tags/{name}").splitlines()]:
        raise Refused(f"origin already has a tag {name}")

    own, lines = signers_at(commit)
    chain = trusted_chain()
    releases = in_chain(chain)
    root_commit, root = root_list()
    since = previous = None
    if new_root:
        made = [link.release for link in chain.links if link.release]
        if not made:
            raise Refused("there is no release chain yet — its first release is made with `tag` alone")
        last = max((link for link in chain.links if link.release), key=lambda link: link.release.version)
        previous = last.release
        if parsed <= previous.version:
            raise Refused(f"{name} is not above {previous.name}, the old chain's last release")
        # A new root is for keys that are lost: one the old list still holds
        # makes launchers take it as an ordinary update, and the notice would
        # tell people the opposite.
        kept = [s for s in own if any(s.key_blob == old.key_blob for old in last.signers)]
        if kept:
            raise Refused(f"not a new root: {', '.join(fingerprint(s.key_blob) for s in kept)} is still on "
                          f"{previous.name}'s list, so launchers would take {name} as an update — sign a rotation "
                          "with `tag --list-change` instead")
        held, before = own, None
        if is_ancestor(previous.commit, commit):
            since = previous.commit
        print(f"A new root: {name} is signed for its own key list, not for {previous.name}'s.")
    elif chain.cut is not None:
        _, fork, _, other, _ = next(entry for entry in chain.forks if entry[2] == chain.cut)
        raise Refused(f"the release chain forks at {chain.links[chain.cut].label}: {fork} and {other} both "
                      "continue it, and launchers below take nothing above it — delete the tag that is not "
                      f"yours from origin (`git push origin :refs/tags/<it>`), then run again")
    elif releases:
        previous, held = releases[-1]
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
        held, before = root, list_text(root_commit)
        print(f"No signed release yet: {name} is the floor, signed for the key list main first carried "
              f"({root_commit[:12]}).")

    stated = re.search(r'^VERSION = "([^"]*)"$', git("show", f"{commit}:desktop/build_common.py"), re.M)
    if not stated or stated.group(1) != name[1:]:
        raise Refused(f"desktop/build_common.py at {commit[:12]} says VERSION = "
                      f"{stated.group(1) if stated else '?'!r}, not {name[1:]!r} — commit the bump first")
    if not own or len(own) != lines or not any(s.allows(release_verify.GIT_NAMESPACE) for s in own):
        raise Refused(f"{updater.SIGNERS_PATH} at {commit[:12]} has {lines} key line(s), {len(own)} of them "
                      f"valid, {sum(s.allows(release_verify.GIT_NAMESPACE) for s in own)} for `git` — "
                      "the next release could not be verified")

    log = git("log", "--oneline", "--no-decorate", f"{since}..{commit}" if since else commit)
    entries = log.splitlines()
    print(f"\n{name} on {commit[:12]} carries {len(entries)} commit(s){' since ' + previous.name if since else ''}:")
    for line in entries[:30]:
        print(f"    {line}")
    if len(entries) > 30:
        print(f"    … and {len(entries) - 30} more")
    if new_root or list_text(commit) != before:
        print(f"\nThe key list changes in this release — these keys sign the NEXT one:")
        print("\n".join(describe(own)))
        if not (list_change or new_root):
            raise Refused("the release changes desktop/release_signers: check the keys above, "
                          "then run again with --list-change")
    print(f"\nThe keys that may sign {name}:")
    print("\n".join(describe(held)))

    if git("config", "--get", "gpg.format", check=False) != "ssh":
        raise Refused("gpg.format is not ssh in this checkout — see docs/RELEASING.md, one-time setup")
    key_type, blob = signing_key()
    if not listed(blob, held, release_verify.GIT_NAMESPACE):
        raise Refused(f"user.signingkey is {key_type} {fingerprint(blob)}, which is not one of the keys above "
                      f"for `git` — launchers would refuse {name}; sign with one of them")
    if not listed(blob, own, release_verify.GIT_NAMESPACE):
        raise Refused(f"{name} would be signed by {key_type} {fingerprint(blob)}, which the key list at "
                      f"{commit[:12]} does not hold for `git`: an installer built from {name} could verify no "
                      "release at all. Sign a rotation with a key the new list keeps (the recovery key), or "
                      "keep this key in the list")
    if new_root:
        print("\n" + new_root_words(name, previous, own))
        answer = f"new root {name}"
        try:
            typed = input(f'\nType "{answer}" to go on: ')
        except EOFError:
            typed = ""
        if typed.strip() != answer:
            raise Refused("not confirmed — nothing was signed")

    print(f"\nSigning {name} on {commit[:12]} — the key may ask for a touch and its PIN.", flush=True)
    signed = subprocess.run(["git", "-C", str(REPO), "tag", "-s", "-m", f"Sautium {name[1:]}", name, commit])
    if signed.returncode != 0:
        raise Refused("git tag -s failed")

    problem = None
    raw = git_bytes("cat-file", "tag", f"refs/tags/{name}")
    try:
        made = release_verify.verify_tag(raw, name, held)
        release_verify.verify_tag(raw, name, own)
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

    oid = git("rev-parse", f"refs/tags/{name}")
    if new_root:
        old = git("config", "--get", FLOOR_CONFIG, check=False)
        git("config", FLOOR_CONFIG, oid)
        print(f"\nThe new root is recorded in this checkout ({FLOOR_CONFIG}, was "
              f"{old[:12] if old else 'derived'}): the chain starts at {name}.")
        print("\nThe public notice for its release notes (scripts/release-publish.sh --notes):\n")
        print(new_root_notice(name, previous, oid, own))
    elif not releases:
        git("config", FLOOR_CONFIG, oid)
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


def sign_file(path: Path) -> None:
    """`<file>.sig` beside it: the file signed under `sautium-file` with this
    checkout's key setup, verified with the launcher's verifier against the
    checkout's own list before it is kept."""
    import tempfile
    program = git("config", "--get", "gpg.ssh.program", check=False) or "ssh-keygen"
    key = git("config", "--get", "user.signingkey", check=False)
    if not key:
        raise Refused("user.signingkey is not set in this checkout — see docs/RELEASING.md, one-time setup")
    keys = signers_at("HEAD")[0]
    key_type, blob = signing_key()
    if not listed(blob, keys, FILE_NAMESPACE):
        raise Refused(f"user.signingkey is {key_type} {fingerprint(blob)}, which {updater.SIGNERS_PATH} does not "
                      f"hold for `{FILE_NAMESPACE}` — nobody could verify the signature with the published list")
    signature = Path(f"{path}.sig")
    with tempfile.TemporaryDirectory() as scratch:
        # ssh-keygen writes <file>.sig beside what it signs and will not
        # overwrite one: it signs a copy in a folder of its own.
        copy = Path(scratch) / path.name
        copy.write_bytes(path.read_bytes())
        if key.startswith("key::"):
            # A literal public key, as git takes it: ssh-keygen signs with the
            # agent's private half when -f names the public one.
            literal = Path(scratch) / "signing-key.pub"
            literal.write_text(key[len("key::"):] + "\n")
            key = str(literal)
        print(f"Signing {path.name} with {program} — the key may ask for a touch and its PIN.", flush=True)
        if subprocess.run([program, "-Y", "sign", "-n", FILE_NAMESPACE, "-f", key, str(copy)]).returncode != 0:
            raise Refused(f"{program} -Y sign failed")
        armored = Path(f"{copy}.sig").read_bytes()
    try:
        signer = release_verify.verify(path.read_bytes(), armored, keys, FILE_NAMESPACE)
    except release_verify.VerifyError as exc:
        raise Refused(f"the launcher's verifier refuses the signature: {exc}")
    signature.write_bytes(armored)
    print(f"{signature} is signed by {', '.join(signer.principals)} ({fingerprint(signer.key_blob)}).")


def verify(version: str, rev: Optional[str]) -> None:
    name, _ = release_name(version)
    fetch()
    found = next((release for release, _ in in_chain(trusted_chain()) if release.name == name), None)
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
            change = p.add_mutually_exclusive_group()
            change.add_argument("--list-change", action="store_true",
                                help="the release changes desktop/release_signers, and that is intended")
            change.add_argument("--new-root", action="store_true",
                                help="every key of the list is lost: start a new chain with new keys")
    p = sub.add_parser("sign-file")
    p.add_argument("file", type=Path)
    p.add_argument("--repo", type=Path, default=ROOT)
    args = parser.parse_args()
    global REPO
    REPO = args.repo.resolve()
    if args.command == "selftest":
        selftest()
    elif args.command == "tag":
        tag(args.version, args.commit, args.list_change, args.new_root)
    elif args.command == "sign-file":
        sign_file(args.file.resolve())
    else:
        verify(args.version, args.commit)


if __name__ == "__main__":
    main()
