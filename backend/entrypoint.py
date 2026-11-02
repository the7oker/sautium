"""Docker entrypoint: drop root, wait for the database, then start uvicorn on
plain HTTP.

The container starts as root only so that this process can hand the node's
own folders to the node's user; it then runs as that user, and so does
everything it starts — the backend, its children, the agent CLIs.

The Web UI rides HTTP on the LAN (PROGRESS.md "HTTP on the LAN"): a
certificate a phone would trust cannot exist for a private address, and
every credential exchange is boxed end to end instead. The peer surface
(p2p_app.py, port 8801) keeps its own TLS, pinned to the node key.
"""

import logging.config
import os
import pwd
import stat
import sys
import time

import psycopg2
import uvicorn
from uvicorn.main import STARTUP_FAILURE

from config import LOGGING_CONFIG, settings
from serving import Server, heard_until_served, served

HOST = os.getenv("UVICORN_HOST", "0.0.0.0")
PORT = int(os.getenv("UVICORN_PORT", "8000"))
RELOAD = os.getenv("UVICORN_RELOAD", "true").lower() in ("1", "true", "yes")

# The node's user (Dockerfile): the backend and the agent CLIs run as it.
NODE_USER = "agent"
# What the node writes, as the compose files mount it. Anything there its
# user cannot write is a root backend's, from before the node ran as its own
# user (or a root `docker exec`'s), and is handed to it at each start. The
# host's ~/.claude and ~/.codex are the owner's and stay as they are: only
# Codex's folder itself, which Docker creates root-owned when the host had
# none, is handed.
_NODE_TREES = ("/app/data", "/app/generated_eq", "/home/agent/.claude/projects")
_NODE_FOLDERS = ("/home/agent/.codex",)
_NODE_CACHE = "/home/agent/.cache"
# Where the model cache was mounted while the backend ran as root: a
# container created from an older compose file still mounts it there.
_ROOT_CACHE = "/root/.cache"


def _read_only(path: str) -> bool:
    return bool(os.statvfs(path).f_flag & os.ST_RDONLY)


def _unwritable(path: str, uid: int) -> bool:
    """Not the user's and not writable by all: a Docker Desktop mount of a
    Windows or macOS folder shows every file writable by all, and is left."""
    st = os.lstat(path)
    return not stat.S_ISLNK(st.st_mode) and st.st_uid != uid and not st.st_mode & stat.S_IWOTH


def _hand_over(pw, trees, folders) -> int:
    """Chown what the node's user cannot write under `trees` and of
    `folders` themselves, read-only mounts aside. The count of paths
    handed."""
    handed = 0

    def take(path: str) -> None:
        nonlocal handed
        if _unwritable(path, pw.pw_uid):
            os.chown(path, pw.pw_uid, pw.pw_gid, follow_symlinks=False)
            handed += 1

    for tree in trees:
        if not os.path.isdir(tree) or _read_only(tree):
            continue
        for top, dirs, files in os.walk(tree):
            dirs[:] = [d for d in dirs if not _read_only(os.path.join(top, d))]
            take(top)
            for name in files:
                take(os.path.join(top, name))
    for folder in folders:
        if os.path.isdir(folder) and not _read_only(folder):
            take(folder)
    return handed


def _can_become_root() -> bool:
    try:
        os.setuid(0)
    except PermissionError:
        return False
    return True


def _reach_the_root_cache() -> None:
    """A container created from an older compose file mounts the model cache
    in root's home. The node's user reaches it there: root's home becomes
    traversable (not readable), and the user's cache is that mount — no
    model is fetched again, and creating the container afresh moves the
    mount itself."""
    os.chmod("/root", 0o711)
    if os.path.islink(_NODE_CACHE):
        return
    if os.path.isdir(_NODE_CACHE):
        os.rmdir(_NODE_CACHE)               # the image's own, empty one
    os.symlink(_ROOT_CACHE, _NODE_CACHE)


def _become_node_user() -> None:
    """Hand the node's folders to its user and drop root, before anything
    else runs. In this process, not by an exec: a process whose user changed
    is no longer dumpable, so the agent CLIs, the same user, can read neither
    its memory nor its environment."""
    if os.geteuid() != 0:
        return
    cache = _NODE_CACHE
    if os.path.ismount(_ROOT_CACHE):
        _reach_the_root_cache()
        cache = _ROOT_CACHE
    pw = pwd.getpwnam(NODE_USER)
    handed = _hand_over(pw, _NODE_TREES + (cache,), _NODE_FOLDERS)
    os.initgroups(pw.pw_name, pw.pw_gid)
    os.setgid(pw.pw_gid)
    os.setuid(pw.pw_uid)
    if _can_become_root():
        raise SystemExit("[entrypoint] root could be taken back after the drop")
    os.environ.update(HOME=pw.pw_dir, USER=pw.pw_name, LOGNAME=pw.pw_name)
    print(f"[entrypoint] running as {pw.pw_name}; {handed} path(s) handed to it", flush=True)


def _wait_for_database() -> None:
    """Block until PostgreSQL accepts a connection.

    Docker's restart policy restores containers without the `depends_on`
    ordering `compose up` honours, so after a daemon restart the backend
    comes up before postgres — seconds before it normally, hours before it
    when the postgres restore itself failed and a human has to press start
    (2026-09-22). Nothing past this point works without the database:
    mb_backend reads it at import, the lifespan migrates it before anything
    serves. So the process waits here, as the launcher's
    service_manager._wait_for_postgres does before it spawns uvicorn, instead
    of starting half-initialized and dying on the first unguarded query.
    Before a connection exists there is no event to subscribe to; the probe
    is the only signal, the same one the healthcheck uses.
    """
    started = time.monotonic()
    delay = 1.0
    last_report = None
    while True:
        try:
            psycopg2.connect(settings.database_url, connect_timeout=5).close()
        except psycopg2.OperationalError as e:
            waited = time.monotonic() - started
            if last_report is None or waited - last_report >= 30:
                reason = str(e).strip().splitlines()[0]
                print(f"[entrypoint] database unreachable, waiting ({waited:.0f}s): {reason}",
                      flush=True)
                last_report = waited
            time.sleep(delay)
            delay = min(delay * 2, 10.0)
            continue
        if last_report is not None:
            print(f"[entrypoint] database reachable after {time.monotonic() - started:.0f}s",
                  flush=True)
        return


def main() -> int:
    _become_node_user()
    # The app's own logging from the first line, as main.py sets it at its
    # import: a stop heard before then is said too
    logging.config.dictConfig(LOGGING_CONFIG)
    heard_until_served()
    _wait_for_database()
    print(f"[entrypoint] uvicorn HTTP on {HOST}:{PORT} (reload={RELOAD})", flush=True)
    served()
    # Without a limit uvicorn waits on open requests until Docker kills the
    # process, and the lifespan's shutdown — a benchmark run putting the
    # owner's HQPlayer back — never runs (exit 137, 2026-10-03: the Web UI's
    # event streams, which serving.Server ends as its stop begins since
    # 2026-10-25; the reload path's own server does not). The compose files
    # give the stop 10 s (stop_grace_period); these 2 are the open requests'
    # share, after the peer surface's 1 (main._PEER_GRACE_S: SIGTERM reaches
    # it first).
    if RELOAD:
        # The development path: the reload supervisor runs the server in a
        # process of its own
        uvicorn.run("main:app", host=HOST, port=PORT, reload=True, timeout_graceful_shutdown=2)
        return 0
    # Built here, not by uvicorn.run, which imports the app before its server
    # holds the signals: PID 1 has no default action for SIGTERM, so a stop
    # during the import was not heard at all
    server = Server(uvicorn.Config("main:app", host=HOST, port=PORT, timeout_graceful_shutdown=2,
                                   log_config=LOGGING_CONFIG))
    try:
        server.run()
    except KeyboardInterrupt:
        pass            # SIGINT, raised again as the server leaves, once stopped in order
    return 0 if server.started else STARTUP_FAILURE


if __name__ == "__main__":
    sys.exit(main())
