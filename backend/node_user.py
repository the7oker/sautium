"""The Docker image's entrypoint: give the node's user the uid of the host
user the node runs for, hand the node's own folders to it, drop root in this
process, then run the container's command as that user.

The image copies this file to a folder of root's and runs it as root with
`python3 -I -S`: no PYTHON* variable, no site-packages, no folder anyone else
can write on its path. It imports the standard library alone, so at the
container's start nothing the node's user could change — /app is the host's
./backend, which that user can write — runs as root. The command it runs
after the drop (`python entrypoint.py`) is the backend, as the node's user.
A `docker exec` without `-u agent` runs as root, and so runs /app's code as
root: the maintenance commands documented in the tree say `-u agent`.
"""

import os
import pwd
import re
import stat
import sys

# The node's user: the backend and the agent CLIs run as it.
NODE_USER = "agent"
# What the node writes, as the compose files mount it. Anything there that is
# not the node user's — a root backend's file from before the node ran as its
# own user, a folder a Windows mount shows as root's — is handed to it at each
# start. The host's ~/.claude and ~/.codex are the owner's and stay as they
# are: only an agent's folder itself while it is root's and empty is handed —
# what Docker creates for a host folder that did not exist (the Linux arm64
# file's ./data/claude; the other files mount projects/ inside ~/.claude, so
# Docker's folder there is never empty). A tree or folder reached through a
# link is not handed (_real_dir): a host whose ~/.claude/projects is itself a
# link has its sessions folder refused, and said, at each start.
NODE_TREES = ("/app/data", "/app/generated_eq", "/home/agent/.cache",
              "/home/agent/.claude/projects")
NODE_FOLDERS = ("/home/agent/.claude", "/home/agent/.codex")
# Where compose files mounted the model cache while the backend ran as root
ROOT_CACHE = "/root/.cache"
# The folder whose owner the node's user becomes. A native Linux host keeps
# its uids across a bind, so the image's fixed uid 1000 is whoever that is on
# the host (a Pi's first user), and the host user running the node loses its
# own folders to it. /app/data is the checkout's backend/data, which the clone
# ships (backend/data/.gitkeep): its owner is the host user the node runs for.
# root's — a folder Docker made, a mount Docker Desktop shows as root's —
# says nothing, and the image's uid stays.
HOST_OWNED = "/app/data"
PASSWD, GROUP = "/etc/passwd", "/etc/group"
MOUNTINFO = "/proc/self/mountinfo"


def _say(message: str) -> None:
    print(f"[node-user] {message}", file=sys.stderr, flush=True)


def _entries(path: str) -> list:
    with open(path, encoding="utf-8") as f:
        return [line.rstrip("\n").split(":") for line in f if line.strip()]


def _write_entries(path: str, entries: list) -> None:
    st = os.stat(path)
    tmp = f"{path}.node-user"
    with open(tmp, "w", encoding="utf-8") as f:
        f.writelines(":".join(e) + "\n" for e in entries)
    os.chmod(tmp, stat.S_IMODE(st.st_mode))
    os.replace(tmp, path)


def _mount_points() -> set:
    points = set()
    with open(MOUNTINFO, encoding="utf-8") as f:
        for line in f:
            # a space, a tab, a newline or a backslash in a path is octal-escaped
            points.add(re.sub(r"\\([0-7]{3})", lambda m: chr(int(m.group(1), 8)), line.split()[4]))
    return points


def _real_dir(path: str) -> bool:
    """A directory reached through no symlink. The node's user can write
    where these paths lead (./backend is /app; ./data/claude holds
    projects/), so a component it swapped for a link would point root's
    chown, or the uid it takes, at a path of its choosing. Nothing of the
    node user's runs while the entrypoint does: a fresh container's start."""
    try:
        return os.path.realpath(path) == path and stat.S_ISDIR(os.lstat(path).st_mode)
    except OSError:
        return False


def _rehome(home: str, old_uid: int, uid: int, gid: int) -> None:
    """The image's own files in the node user's home follow its new uid;
    whatever is mounted there (the model cache, a host's ~/.claude) is not
    walked — the hand-over takes the node's trees, a host's stay its own."""
    mounts = _mount_points()
    for top, dirs, files in os.walk(home):
        dirs[:] = [d for d in dirs if os.path.join(top, d) not in mounts]
        for path in [top, *(os.path.join(top, name) for name in files)]:
            st = os.lstat(path)
            if st.st_uid == old_uid:
                os.chown(path, uid, gid, follow_symlinks=False)


def take_host_owner(pw, folder: str, passwd: str, group: str):
    """The node's user renumbered to the uid (and, unless it is a group of
    the image's, the gid) of `folder`'s owner, when that owner is neither root nor the node's
    user already; the node user's passwd entry after. A uid another user of
    the image holds leaves the node's as it is, and says so. Idempotent: the
    next start finds the uid taken."""
    try:
        st = os.lstat(folder)
    except FileNotFoundError:
        return pw
    except OSError as e:
        _say(f"{folder} could not be read ({e.strerror or e}): the node's user stays uid {pw.pw_uid}")
        return pw
    if not _real_dir(folder):
        _say(f"{folder} is reached through a link or is no folder: the node's user stays uid {pw.pw_uid}")
        return pw
    if st.st_uid in (0, pw.pw_uid):
        return pw
    users = _entries(passwd)
    holder = next((e[0] for e in users if e[2] == str(st.st_uid) and e[0] != pw.pw_name), None)
    if holder:
        _say(f"{folder} is uid {st.st_uid}'s, which is {holder} in the image: "
             f"the node's user stays uid {pw.pw_uid}")
        return pw
    # A gid of the image's own groups (root, shadow, adm … and users, which a
    # host's own users group may share) is never the node user's: its own
    # group keeps its number then — a wrong group, never a privilege
    groups = _entries(group)
    taken = any(e[2] == str(st.st_gid) and e[0] != pw.pw_name for e in groups)
    gid = pw.pw_gid if st.st_gid == 0 or taken else st.st_gid
    for e in users:
        if e[0] == pw.pw_name:
            e[2], e[3] = str(st.st_uid), str(gid)
    for e in groups:
        if e[0] == pw.pw_name and e[2] == str(pw.pw_gid):
            e[2] = str(gid)
    _write_entries(group, groups)
    _write_entries(passwd, users)
    _rehome(pw.pw_dir, pw.pw_uid, st.st_uid, gid)
    _say(f"the node's user takes uid {st.st_uid}, gid {gid}: the owner of {folder}")
    return pwd.struct_passwd((pw.pw_name, pw.pw_passwd, st.st_uid, gid,
                              pw.pw_gecos, pw.pw_dir, pw.pw_shell))


def hand_over(pw, trees, folders) -> tuple:
    """Chown to the node's user what is not its own under `trees`, and each
    of `folders` itself while it is root's and empty — what Docker makes for
    a host folder that did not exist; read-only mounts and symlinks left as
    they are. (handed, refused): a path the filesystem would not read or
    hand over — a drvfs mount's passing EIO, a file gone meanwhile — is
    counted, not fatal, and the first is said with why; a folder left as
    another user's is said too. What the node's user then cannot write
    fails where it is written."""
    handed = refused = 0

    def refuse(error: OSError) -> None:
        nonlocal refused
        if not refused:
            _say(f"not handed over: {error.filename or '?'}: {error.strerror or error}")
        refused += 1

    def writable(path: str) -> bool:
        try:
            return not os.statvfs(path).f_flag & os.ST_RDONLY
        except OSError as e:
            refuse(e)
            return False

    def take(path: str) -> None:
        nonlocal handed
        try:
            st = os.lstat(path)
            if stat.S_ISLNK(st.st_mode) or st.st_uid == pw.pw_uid:
                return
            os.chown(path, pw.pw_uid, pw.pw_gid, follow_symlinks=False)
            handed += 1
        except OSError as e:
            refuse(e)

    for tree in trees:
        try:
            os.lstat(tree)
        except FileNotFoundError:
            continue                        # not mounted on this node
        except OSError as e:
            refuse(e)
            continue
        if not _real_dir(tree):
            _say(f"not handed over: {tree} is reached through a link or is no folder")
            continue
        if not writable(tree):
            continue
        for top, dirs, files in os.walk(tree, onerror=refuse):
            dirs[:] = [d for d in dirs if writable(os.path.join(top, d))]
            take(top)
            for name in files:
                take(os.path.join(top, name))
    for folder in folders:
        try:
            st = os.lstat(folder)
            if not _real_dir(folder) or st.st_uid == pw.pw_uid or not writable(folder):
                continue
            empty = not os.listdir(folder)
        except FileNotFoundError:
            continue
        except OSError as e:
            refuse(e)
            continue
        if st.st_uid == 0 and empty:
            take(folder)
        else:
            _say(f"{folder} is uid {st.st_uid}'s{'' if empty else ' and holds files'}: "
                 f"left as it is, which uid {pw.pw_uid} may not write")
    return handed, refused


def can_become_root() -> bool:
    try:
        os.setuid(0)
    except PermissionError:
        return False
    return True


def become_node_user(trees, folders):
    """Give the node's user the uid of the host user its folders are for,
    hand those folders over, then drop root for good in this process; the
    node user's passwd entry."""
    pw = take_host_owner(pwd.getpwnam(NODE_USER), HOST_OWNED, PASSWD, GROUP)
    handed, refused = hand_over(pw, trees, folders)
    os.initgroups(pw.pw_name, pw.pw_gid)
    os.setgid(pw.pw_gid)
    os.setuid(pw.pw_uid)
    if can_become_root():
        raise SystemExit("[node-user] root could be taken back after the drop")
    os.environ.update(HOME=pw.pw_dir, USER=pw.pw_name, LOGNAME=pw.pw_name)
    _say(f"running as {pw.pw_name} (uid {pw.pw_uid}): {handed} path(s) handed to it"
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
