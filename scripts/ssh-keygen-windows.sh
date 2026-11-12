#!/usr/bin/env bash
# git's gpg.ssh.program in WSL for a key that only Windows' own OpenSSH can
# reach — a FIDO2 key on a security key, which it drives through Windows
# Hello. git hands its ssh-keygen POSIX paths (the signing buffer under /tmp,
# the key, the allowed-signers file); this passes each one that exists on this
# side to Windows' ssh-keygen.exe in its Windows form (\\wsl.localhost\... for
# WSL's own files) and everything else as it is. The .sig ssh-keygen.exe
# writes beside the buffer is where git reads it back. Its answers end their
# lines in CRLF, and git reads the principal `-Y find-principals` prints up to
# the LF only — a principal ending in CR, under which `-Y verify` then finds
# no key — so the CRs are dropped on the way out.
#
#   git config gpg.ssh.program "$PWD/scripts/ssh-keygen-windows.sh"
#
# SAUTIUM_SSH_KEYGEN_EXE names another ssh-keygen.exe (a newer Win32-OpenSSH).
set -euo pipefail

exe=${SAUTIUM_SSH_KEYGEN_EXE:-/mnt/c/Windows/System32/OpenSSH/ssh-keygen.exe}
args=()
for arg in "$@"; do
    if [[ $arg == /* && -e $arg ]]; then
        args+=("$(wslpath -w "$arg")")
    else
        args+=("$arg")
    fi
done
"$exe" "${args[@]}" | tr -d '\r'
exit "${PIPESTATUS[0]}"
