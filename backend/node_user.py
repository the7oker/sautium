"""The Docker image's entrypoint: hand the node's own folders to the node's
user, drop root in this process, then run the container's command as that
user.

The image copies this file to a folder of root's and runs it as root with
`python3 -I -S`: no PYTHON* variable, no site-packages, no folder anyone else
can write on its path. It imports the standard library alone, so at the
container's start nothing the node's user could change — /app is the host's
./backend, which that user can write — runs as root. The command it runs
after the drop (`python entrypoint.py`) is the backend, as the node's user.
A `docker exec` is root's unless it says `-u agent`, and then runs /app's
code as root: a hand-run command says `-u agent`.
"""

import os
import pwd
import stat
import sys

# The node's user: the backend and the agent CLIs run as it.
NODE_USER = "agent"
# What the node writes, as the compose files mount it. Anything there that is
# not the node user's — a root backend's file from before the node ran as its
# own user, a folder a Windows mount shows as root's — is handed to it at each
# start. The host's ~/.claude and ~/.codex are the owner's and stay as they
# are: only Codex's folder itself, which Docker creates root-owned when the
# host had none, is handed.
NODE_TREES = ("/app/data", "/app/generated_eq", "/home/agent/.cache",
              "/home/agent/.claude/projects")
NODE_FOLDERS = ("/home/agent/.codex",)
# Where compose files mounted the model cache while the backend ran as root
ROOT_CACHE = "/root/.cache"


def _say(message: str) -> None:
    print(f"[node-user] {message}", file=sys.stderr, flush=True)


def hand_over(pw, trees, folders) -> tuple:
    """Chown to the node's user what is not its own under `trees`, and each
    of `folders` itself while it is root's; read-only mounts and symlinks
    left as they are. (handed, refused): a path the filesystem would not
    read or hand over — a drvfs mount's passing EIO, a file gone meanwhile —
    is counted, not fatal; what the user then cannot write fails where it is
    written."""
    handed = refused = 0

    def refuse(_error=None) -> None:
        nonlocal refused
        refused += 1

    def writable(path: str) -> bool:
        try:
            return not os.statvfs(path).f_flag & os.ST_RDONLY
        except OSError:
            refuse()
            return False

    def take(path: str, roots_only: bool = False) -> None:
        nonlocal handed
        try:
            st = os.lstat(path)
            if (stat.S_ISLNK(st.st_mode) or st.st_uid == pw.pw_uid
                    or (roots_only and st.st_uid != 0)):
                return
            os.chown(path, pw.pw_uid, pw.pw_gid, follow_symlinks=False)
            handed += 1
        except OSError:
            refuse()

    for tree in trees:
        if not os.path.isdir(tree) or not writable(tree):
            continue
        for top, dirs, files in os.walk(tree, onerror=refuse):
            dirs[:] = [d for d in dirs if writable(os.path.join(top, d))]
            take(top)
            for name in files:
                take(os.path.join(top, name))
    for folder in folders:
        if os.path.isdir(folder) and writable(folder):
            take(folder, roots_only=True)
    return handed, refused


def can_become_root() -> bool:
    try:
        os.setuid(0)
    except PermissionError:
        return False
    return True


def become_node_user(trees, folders):
    """Hand the node's folders over, then drop root for good in this process;
    the node user's passwd entry."""
    pw = pwd.getpwnam(NODE_USER)
    handed, refused = hand_over(pw, trees, folders)
    os.initgroups(pw.pw_name, pw.pw_gid)
    os.setgid(pw.pw_gid)
    os.setuid(pw.pw_uid)
    if can_become_root():
        raise SystemExit("[node-user] root could be taken back after the drop")
    os.environ.update(HOME=pw.pw_dir, USER=pw.pw_name, LOGNAME=pw.pw_name)
    _say(f"running as {pw.pw_name}: {handed} path(s) handed to it"
         + (f", {refused} the filesystem would not hand over" if refused else ""))
    return pw


def main(argv) -> None:
    if not argv:
        raise SystemExit("[node-user] no command to run")
    if os.geteuid() == 0:
        if os.path.ismount(ROOT_CACHE):
            _say(f"the model cache is mounted at {ROOT_CACHE}, where the node's user does not "
                 "read it: mount it at /home/agent/.cache, as the compose files do")
        become_node_user(NODE_TREES, NODE_FOLDERS)
    os.execvp(argv[0], argv)


if __name__ == "__main__":
    main(sys.argv[1:])
