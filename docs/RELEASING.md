# Releasing Sautium

An installed launcher takes **releases**, never the tip of a branch. A release
is a signed annotated tag `v<major>.<minor>.<patch>` on a commit of `main`. A
push to `main` reaches nobody until a release key signs a tag on it. This page
is the maintainer's procedure. `scripts/release.py` runs the checks; the code
it relies on is `desktop/release_verify.py` and `desktop/updater.py`.

## What a launcher accepts

- **The signature.** An SSH signature (SSHSIG, namespace `git`) over the tag
  object, made by `git tag -s` with `gpg.format=ssh`. The launcher verifies it
  in its own Python, with `cryptography`, not with `git verify-tag`: the
  Windows installer's MinGit has no `ssh-keygen`.
- **Two key types, and nothing else:**
  - `ssh-ed25519`;
  - `sk-ssh-ed25519@openssh.com`, a FIDO2 key on a security key. Its signature
    counts only if the authenticator says it was touched **and** its PIN was
    entered (the user-presence and user-verification flags, `0x05`). A touch
    alone is refused.

  RSA, ECDSA and certificates are refused.
- **The keys.** They come from `desktop/release_signers`, in git's
  allowed-signers format: `principal namespaces="git" <type> <key> [comment]`,
  one line per key. `namespaces=` may name several, comma-separated: the daily
  key's line says `namespaces="git,sautium-file"`, because it also signs the
  installers' checksums (below). A tag counts only under `git`. A line with any
  other option, or one that does not parse, is refused alone and the refusal
  is logged.
- **Whose list: the chain.** The launcher walks the releases above the one it
  runs, oldest version first. Each release is verified against the list of
  the release before it on that walk: the installed tree's own list
  (`git show HEAD:desktop/release_signers`) for the first step, then the list
  each release it reached carries. A release is never verified against its
  own list. What it offers is the highest release so reached that the
  installed list verifies as well. A first install walks from the installer's
  own release, with the list in its payload.
- **What a rotation does.** A release may change the list. A key it drops can
  sign nothing above it, for every launcher, also one still below it. Such a
  launcher takes the rotating release first, and the releases signed by the
  new key at its next check: two updates, by design.
- **The name.** The tag object's own `tag` header must equal the name it was
  fetched under, and the version compared is that name. A signed `v0.2.0`
  pushed as `v9.9.9` is refused.
- **The history.** Each release on the walk must descend from the release
  before it, the first from HEAD. The offer shows as "Sautium \<version\>". A
  version below the installed one is never taken.
- **A fork.** Two signed releases that continue the same release, neither
  descending from the other, are a fork. The launcher cannot tell which one is
  yours, because a thief may give theirs any version, a lower one too. So it
  takes nothing above the release they both continue, and the dialog names
  the two. Deleting the tag that is not yours from origin ends it.
- **A rewrite.** A newer signed release that does not descend from HEAD (a
  rewritten history) is refused. The dialog says that reinstalling moves to
  it, and nothing is purged.
- **The floor.** `v0.1.0` and `v0.1.1` are lightweight and unsigned, made
  before releases were signed. Nothing at or below `0.1.1` is ever a release.
  Until the first signed tag exists, a launcher running this code is offered
  nothing.

Launchers older than this code follow `origin/main`. They reach this code the
old way, and from then on take signed tags only. That is why the first signed
release follows the push that ships this code on the same day.

A developer's working tree runs the same updater. If it carries commits of its
own, no release descends from it, so it is never moved. A tracked file edited
by hand stops the update as well. Docker nodes have no updater.

## The keys

Two keys, in two roles. Their lines in `desktop/release_signers`:

    release namespaces="git,sautium-file" sk-ssh-ed25519@openssh.com AAAAGnNrLXNzaC1lZDI1NTE5QG9wZW5zc2guY29tAAAAINFPSGUPTG3nUxaoM63ZGdZq/mwg4XMbkS8AEaXhD1wiAAAAE3NzaDpzYXV0aXVtLXJlbGVhc2U= sautium release (yubikey)
    recovery namespaces="git" ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIEAYdqHtNHo79nrVNGQMz5Irv3K5MUV8nkXn+tHc0zfJ sautium release recovery


- **The daily key** signs every release. It is a FIDO2 key resident on a
  security key, made with `verify-required`, so every signature needs a touch
  and the PIN:

      ssh-keygen -t ed25519-sk -O resident -O verify-required -O application=ssh:sautium-release -C "sautium release"

- **The recovery key** is a plain `ssh-ed25519` key with a passphrase, kept
  offline. It signs only a release that changes the list, for example one that
  names a new daily key after the old one is lost. Its line allows `git`
  alone: it never signs the checksums.

Each key has one line in `desktop/release_signers`. The line goes in through
an ordinary commit, which review sees.

## One-time setup

Set this up on the machine you sign on, inside the checkout you tag from.
Every variant ends with the same check:

    python scripts/release.py selftest

The self-test signs a scratch file the way `git tag -s` would, under the
throwaway namespace `sautium-test`. It then verifies the result with the
launcher's own verifier against the checkout's list. If a security key answers
without its PIN, the self-test fails, just as a launcher would refuse the
release. The script needs a Python with `cryptography`, for example the
checkout's `.venv`.

### Windows: signing from WSL through Windows' own OpenSSH

On Windows, the security key is reached by Windows' own OpenSSH
(`C:\Windows\System32\OpenSSH\ssh-keygen.exe`) through Windows Hello. WSL's
`ssh-keygen` and Git for Windows' cannot reach it without extra setup. This
path keeps git, the checkout and the push in WSL. Signing goes to Windows'
`ssh-keygen.exe` through `scripts/ssh-keygen-windows.sh`, which converts the
paths.

1. Download the key handle into your Windows profile, from PowerShell:

       cd $env:USERPROFILE\.ssh
       ssh-keygen -K

   `-K` downloads every resident key on the security key. Find yours by
   fingerprint:

       ssh-keygen -lf id_ed25519_sk_rk_sautium-release.pub

   The handle must stay in the Windows profile. Windows' OpenSSH refuses a key
   file whose permissions it cannot read as private, and files under
   `\\wsl.localhost` are such files.
2. In the checkout, under WSL:

       git config gpg.format ssh
       git config user.signingkey /mnt/c/Users/<you>/.ssh/id_ed25519_sk_rk_sautium-release
       git config gpg.ssh.program "$PWD/scripts/ssh-keygen-windows.sh"
       git config gpg.ssh.allowedSignersFile "$PWD/desktop/release_signers"

3. Run `python scripts/release.py selftest`. The Windows Security dialog asks
   for the security key: touch it and enter its PIN.

The other way is Git for Windows in a Windows checkout:
`git config gpg.ssh.program C:/Windows/System32/OpenSSH/ssh-keygen.exe`, with
the key path in its Windows form and a Windows Python with `cryptography`.

**Windows Hello issues in Win32-OpenSSH**, both open as of 2026-11:

- [#2408](https://github.com/PowerShell/Win32-OpenSSH/issues/2408): since
  Windows 11 25H2, using an `-sk` key may get a dialog that offers a phone or
  a security key instead of Windows Hello. Stale `ssh:` passkeys are one known
  cause. Delete them in Settings › Accounts › Passkeys (or in Yubico
  Authenticator › Passkeys) and try again.
- [#2413](https://github.com/PowerShell/Win32-OpenSSH/issues/2413): creating
  an `ed25519-sk` key through Windows Hello may ask to insert a security key
  and never ask for a PIN.

In both cases, the self-test is the check. A key made without
`verify-required` signs with a touch alone, and the launcher refuses it.

**Windows never overwrites a resident key.** Each
`ssh-keygen -t ed25519-sk -O resident` made through Windows Hello adds a new
credential, even after "Overwrite key in token? y". Two `ssh:sautium-release`
entries can then sit side by side. When you rotate or replace the key:

- delete the old entry in Yubico Authenticator › Passkeys;
- tell same-named keys apart by fingerprint (`ssh-keygen -K` downloads all of
  them).

### macOS: Homebrew's OpenSSH

The `ssh-keygen` that ships with macOS cannot drive a FIDO2 key. Homebrew's
can:

    brew install openssh
    cd ~/.ssh && /opt/homebrew/bin/ssh-keygen -K      # the security key's PIN
    git config gpg.format ssh
    git config user.signingkey ~/.ssh/id_ed25519_sk_rk_sautium-release
    git config gpg.ssh.program /opt/homebrew/bin/ssh-keygen
    git config gpg.ssh.allowedSignersFile "$PWD/desktop/release_signers"
    python3 scripts/release.py selftest

On an Intel Mac, Homebrew lives under `/usr/local`.

### Linux or WSL: the system's OpenSSH

This variant suits a software key, such as the recovery key, or a security key
the Linux side can reach (on WSL that needs `usbipd`):

    git config gpg.format ssh
    git config user.signingkey ~/.ssh/<key>
    git config --unset gpg.ssh.program                 # ssh-keygen from PATH
    git config gpg.ssh.allowedSignersFile "$PWD/desktop/release_signers"
    python3 scripts/release.py selftest

## Each release

1. Set `VERSION` in `desktop/build_common.py` to the new version, then commit
   and push `main`. The script refuses a tag whose tree states another
   version, because the installers are named after it.
2. Sign the tag:

       python scripts/release.py tag <version>          # e.g. 0.2.0

   The script checks that:
   - the commit is your checkout's HEAD (unless `--commit` names another) and
     is on `origin/main`. It is never origin's tip by default, because the tip
     is exactly what a push nobody intended controls;
   - the version is new and above the last signed release;
   - the tree's `desktop/release_signers` parses whole and holds a key for
     `git`, since it is the list the next release is verified against;
   - the commit descends from the last signed release, and the chain does not
     fork;
   - the key `user.signingkey` names is on the last release's list **and** on
     the tree's own list. A release whose own list drops its signer leaves
     every installer built from it unable to verify anything. Both checks run
     before the signature, so a refusal costs no touch.

   It prints the commits the release carries since the last one. If the key
   list changes in this release, it prints the new keys with their
   fingerprints and stops until you run it again with `--list-change`.

   It then runs `git tag -s` (touch and PIN) and verifies the tag with the
   launcher's own verifier against the last signed release's list and against
   its own. It runs `git verify-tag` too. If any check refuses, the tag is
   deleted again. **It never pushes.**
3. Push the tag with the command it prints:

       git push origin refs/tags/v<version>

4. Build the installers from the tag: `git worktree add --detach <dir>
   v<version>`, then `desktop/build_windows.py` and `desktop/build_macos.py`
   in it.
5. Publish them with `scripts/release-publish.sh --tree <dir> <installers>`.
   It stops unless origin's `v<version>` is a signed release launchers take,
   on the very commit the installers were built from. Creating a GitHub
   release for a tag origin does not have makes GitHub create a lightweight,
   unsigned tag of that name, and the name is then taken for good. It signs
   `SHA256SUMS.txt` with the daily key (a second touch and PIN, below) and
   uploads `SHA256SUMS.txt.sig` beside it.

`python scripts/release.py verify <version>` says at any time whether origin's
tag is in the release chain. The script walks the chain with the launcher's
own code (`desktop/updater.py`, `walk_chain`), so it never accepts a release
launchers refuse, nor the reverse. Each release in the chain is signed by a
key on the list of the release before it and descends from it, and nothing
above a fork is in it. The chain starts at the floor, the first signed release
(or the last new root, below). `tag` records the floor it makes in the
checkout's git configuration (`sautium.releaseFloor`). From then on that
record is the root, and no push can reshape it (a merge whose first parent is
a history of the pusher's own, for example). A checkout without the record,
such as an auditor's clone, derives the root from the first key list on
`origin/main`'s first-parent line, and says so.

## Rotation and recovery

- **A new key** reaches launchers in a release that a key already on the list
  signed, and whose tree lists the new key. The release after that may be
  signed by the new key alone. A launcher below the rotation takes it in two
  updates: the rotating release, then the new key's.
- **Sign a rotation with a key the new list keeps**, such as the recovery key,
  which stays listed. `release.py tag` refuses a release whose own list drops
  the key that signs it: the installers built from it would have nothing to
  verify, and a first install from one of them would fall back to its bundled
  copy.
- **The daily key is lost:** the recovery key signs a release whose list names
  a new daily key.
- **The daily key is stolen:** the same rotation, plus a public notice. The
  thief still needs a push to GitHub to reach anyone, so the account's
  hardening is the other half of the defence. Once the rotation is on origin,
  a release the stolen key signs above it is refused by every launcher, also
  by one that has not taken the rotation yet. A tag the stolen key signs
  beside it — on the history before the rotation, at any version — is a fork:
  launchers below the rotation take nothing until you delete that tag from
  origin, and launchers that took the rotation are not affected.
- **Both keys are lost:** a new root, below.

### Both keys lost: a new root

Installed launchers verify every release against keys they already trust, so a
release signed only by new keys reaches none of them. That is the protection
working. People move on by installing again, from an installer built at the new
release: its payload carries the new list.

1. Make two new keys (as above), and commit a `desktop/release_signers` that
   lists only them, with the version bump.
2. Sign the release as a new root:

       python scripts/release.py tag <version> --new-root

   The version must be above the old chain's last release. The release is
   signed by a key of its own new list and need not descend from the last
   release. The script prints what launchers will do and asks you to type
   `new root v<version>`. It then signs, verifies the tag against the new
   list, records it as this checkout's floor (`sautium.releaseFloor`, the old
   value printed) and prints the public notice for the release notes, with
   the new keys' fingerprints and the new floor's tag object id.
3. Push the tag, build the installers from it, and publish them with
   `scripts/release-publish.sh --notes <the notice> …`. It accepts the new
   root's installers because the checkout's floor is the new root.
4. An auditor's clone verifies the new chain once it records the same floor:
   `git config sautium.releaseFloor <the tag object id in the notice>`.

From then on `tag` works as before, on the new chain.
- **How a reinstall moves an install.** An installer run over an existing
  install moves it only if the installer is **newer** than the release the
  install holds, and then only to a newer release still, the newest that the
  installer's own list verifies. Both versions are read from what each tree
  states. An installer as old as the install, or older, changes nothing: its
  list must never undo a rotation the install has already taken. The same
  move takes an install past a rewritten history, as long as the installer is
  built from a release on the new history.

## The installers' checksums

`scripts/release-publish.sh` writes `SHA256SUMS.txt` and signs it:
`python scripts/release.py sign-file SHA256SUMS.txt` signs under the namespace
`sautium-file` with the same `gpg.ssh.program` and `user.signingkey` as the tag
(a touch and the PIN), then verifies the signature with the launcher's verifier
against the tree's list before it keeps `SHA256SUMS.txt.sig`. A signature under
`sautium-file` can never pass for a release: a tag counts only under `git`.

A person checks a download with the daily key's line, saved as
`release_signers`. It is `desktop/release_signers` at the release's tag on
GitHub, it is in the README, and it is on the site's download page; compare
two of them. Then, beside the files:

    ssh-keygen -Y verify -f release_signers -I release -n sautium-file -s SHA256SUMS.txt.sig < SHA256SUMS.txt
    sha256sum -c SHA256SUMS.txt --ignore-missing

The first line answers `Good "sautium-file" signature for release`, and the
fingerprint it names is `SHA256:VfUGpBDGFCxAw2J4n8WNeFJj/LcrTac5Zgp73egmpqU`. On Windows
it runs as it is in `cmd` (Windows 10 and later carry OpenSSH); PowerShell has
no `<`, so there it is `cmd /c "…"`. macOS's own `ssh-keygen` verifies it too:
checking a signature needs no security key. The tests run this line against
Linux's `ssh-keygen` and Windows' `ssh-keygen.exe`.

## On GitHub

A tag that is not a release costs a launcher little. Names of any other form
are never fetched, and a tag that does not verify brings its tag object and
commit, but neither history nor trees. What a tag object names directly is
still sent whole: a pushed tag on a large blob costs that blob once.

A ruleset on tags matching `v*` should forbid deleting and updating them. A
launcher judges a moved tag as it now stands, so a moved tag never installs
anything. The ruleset makes the attempt fail visibly instead.
