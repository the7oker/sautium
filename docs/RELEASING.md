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
  one line per key. A line with any other option, or one that does not parse,
  is refused alone and the refusal is logged.
- **Whose list.** The launcher always reads the **installed** tree's list
  (`git show HEAD:desktop/release_signers`), never the one in the release it
  is about to take. A release may change the list; the new list then governs
  the next update. A first install reads the list in the installer's payload.
- **The name.** The tag object's own `tag` header must equal the name it was
  fetched under, and the version compared is that name. A signed `v0.2.0`
  pushed as `v9.9.9` is refused.
- **The history.** The release's commit must descend from HEAD. Of the signed
  releases that do, the highest version is offered as "Sautium \<version\>".
  A version below the installed one is never taken.
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

Two keys, in two roles:

- **The daily key** signs every release. It is a FIDO2 key resident on a
  security key, made with `verify-required`, so every signature needs a touch
  and the PIN:

      ssh-keygen -t ed25519-sk -O resident -O verify-required -O application=ssh:sautium-release -C "sautium release"

- **The recovery key** is a plain `ssh-ed25519` key with a passphrase, kept
  offline. It signs only a release that changes the list, for example one that
  names a new daily key after the old one is lost.

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
   - the commit descends from the last signed release.

   It prints the commits the release carries since the last one. If the key
   list changes in this release, it prints the new keys with their
   fingerprints and stops until you run it again with `--list-change`.

   It then runs `git tag -s` (touch and PIN) and verifies the tag with the
   launcher's own verifier against the last signed release's list. It runs
   `git verify-tag` too. If either check refuses, the tag is deleted again.
   **It never pushes.**
3. Push the tag with the command it prints:

       git push origin refs/tags/v<version>

4. Build the installers from the tag: `git worktree add --detach <dir>
   v<version>`, then `desktop/build_windows.py` and `desktop/build_macos.py`
   in it.
5. Publish them with `scripts/release-publish.sh --tree <dir> <installers>`.
   It stops unless origin's `v<version>` is a signed release launchers take,
   on the very commit the installers were built from. Creating a GitHub
   release for a tag origin does not have makes GitHub create a lightweight,
   unsigned tag of that name, and the name is then taken for good.

`python scripts/release.py verify <version>` says at any time whether origin's
tag is in the release chain. Each release in the chain is signed by a key on
the list of the release before it and descends from it. The chain starts at
the floor, the first signed release. `tag` records the floor it makes in the
checkout's git configuration (`sautium.releaseFloor`). From then on that
record is the root, and no push can reshape it (a merge whose first parent is
a history of the pusher's own, for example). A checkout without the record,
such as an auditor's clone, derives the root from the first key list on
`origin/main`'s first-parent line, and says so.

## Rotation and recovery

- **A new key** reaches launchers in a release that a key already on the list
  signed, and whose tree lists the new key. The release after that may be
  signed by the new key alone.
- **The daily key is lost:** the recovery key signs a release whose list names
  a new daily key.
- **The daily key is stolen:** the same rotation, plus a public notice. The
  thief still needs a push to GitHub to reach anyone, so the account's
  hardening is the other half of the defence. A launcher judges each release
  against its own installed list, so one that has not yet taken the release
  that rotates the key out still trusts the old key. Rotating soon after a
  theft is what limits that window.
- **Both keys are lost:** installed launchers stop taking updates. A reinstall
  from a new installer moves them on, because its payload carries the new
  list.
- **How a reinstall moves an install.** An installer run over an existing
  install moves it only if the installer is **newer** than the release the
  install holds, and then only to a newer release still, the newest that the
  installer's own list verifies. Both versions are read from what each tree
  states. An installer as old as the install, or older, changes nothing: its
  list must never undo a rotation the install has already taken. The same
  move takes an install past a rewritten history, as long as the installer is
  built from a release on the new history.

## On GitHub

A tag that is not a release costs a launcher little. Names of any other form
are never fetched, and a tag that does not verify brings its tag object and
commit, but neither history nor trees. What a tag object names directly is
still sent whole: a pushed tag on a large blob costs that blob once.

A ruleset on tags matching `v*` should forbid deleting and updating them. A
launcher judges a moved tag as it now stands, so a moved tag never installs
anything. The ruleset makes the attempt fail visibly instead.
