"""Sautium's host agent: what a Docker node on Linux cannot do for itself,
done on the machine at the node's request (PI-NODE-1 F2; PI-NODE-2A).

The node asks by the NAME of an empty file in ./run/requests — `check`,
`update` or `diagnostics` — and nothing else: the agent reads no content,
takes no path, URL, tag, commit, value or command from the node. Whatever it
needs it reads from files no container can write: the checkout's tracked
tree and .git (both read-only in the node), .env, and its own folder ./host/,
which no container mounts.

**The rule it rests on: the host's side never opens a path a container can
write.** The requests folder is the one such path it touches, and only so:
opened by a handle without following a link, its entries removed by
handles — a link unlinked, never followed; a FIFO unlinked, never opened; a
folder removed bottom-up to a bounded depth — and their names read. What it
writes is under ./host/. The answer goes back through the node's own API on
loopback, signed with the channel's key (./host/agent.env, which the node
also gets in its environment: it verifies answers with it, and a node an
attacker controls gains nothing by knowing it), never with the node's
.api_secret, which lives where the node writes; the agent is then a client
of a server that may be hostile: no redirect followed, a short timeout, the
response's body ignored.

Two parts (PI5-PLAN 2026-11-15). The STARTER, ./host/bin/sautium-host-agent,
bound into no container: a few lines that run this file under `python3 -I`.
The BODY, this file, in the checkout: it moves with every release. The
starter–body interface is versioned (STARTER_API) and kept compatible both
ways: a starter of any version runs `<body> run` with
SAUTIUM_HOST_STARTER_API in its environment, and a body takes any version of
it. The body replaces the starter only from a release the agent itself
verified and moved to, only once that body has run healthy for
STARTER_AFTER, never in the run that moved it there; the swap is one
rename, the previous starter kept beside it, and the floor that never
updates is the user manager's `OnFailure=`: its unit (written by `install`,
never rewritten by a body) puts the previous starter back after
STARTER_TRIES failed starts.

Units are the person's (`systemctl --user`): a path unit on the requests
folder (`DirectoryNotEmpty=`), its one-shot service, a probe service that
tries a new starter at once, and the restore unit. Lingering
(`loginctl enable-linger`) keeps them alive with nobody logged in: the one
root line, the owner's.

    python3 -I scripts/host_agent.py install      # as the person who owns the checkout
    python3 -I scripts/host_agent.py uninstall
    python3 -I scripts/host_agent.py status

Every body answers `selftest` (an update keeps a release only once its body
starts) and `run`.

The standard library only, Python 3.9's language: a host's own Python.
"""

import argparse
import errno
import fcntl
import hashlib
import hmac
import json
import os
import re
import secrets
import shlex
import signal
import stat
import subprocess
import sys
import time
import traceback
import urllib.error
import urllib.request
from pathlib import Path
from typing import Dict, List, Optional, Tuple

BODY_VERSION = 1
# The interface a starter speaks: `<body> run` with this in the environment.
# A body takes every version from 1 on; a new version may add, never remove.
STARTER_API = 1
VERBS = ("check", "update", "diagnostics")
# The order a run takes them in: the update last, since it restarts the node
ORDER = ("check", "diagnostics", "update")

ROOT = Path(__file__).resolve().parent.parent
COMPOSE_FILE = "docker-compose.pi.yml"
DOCKERFILE = "Dockerfile.mac"           # docker-compose.pi.yml's (a test holds them together)
# What the image carries, by the Dockerfile's argument and the key of .env the
# console setup recorded the choice under (docker-compose.pi.yml reads the
# same keys; a test holds the three together). .env is the person's, mounted
# into no container: an update builds what the install built.
IMAGE_ARGS = (("MODELS", "SAUTIUM_IMAGE_MODELS"), ("ASSISTANT_CLI", "SAUTIUM_IMAGE_ASSISTANT_CLI"),
              ("STREAMING", "SAUTIUM_IMAGE_STREAMING"))
BACKEND, POSTGRES = "sautium-backend", "sautium-postgres"
DEFAULT_IMAGE = "sautium-backend:local"
NODE_API = "http://127.0.0.1:8800"
ANSWER_PATH = "/api/host/answer"
LABEL = "com.docker.compose.project=sautium-host-agent"
RELEASE_REFS = "refs/sautium/releases/"

# The floors (PI-NODE-1 F2): what a forged request costs at most
CHECK_FLOOR = 3600                       # a fetch from the project's own remote, at most hourly
UPDATE_NOOP_FLOOR = 24 * 3600            # not again within a day of one that changed nothing
DIAGNOSTICS_FLOOR = 600
HELD_FLOOR = 600                         # after a refusal for the checkout's own files: the person fixes them
RETRY_BACK = 3600                        # a way back whose database did not go back, tried again
# The requests folder: what one run removes at most, and how deep
DRAIN_MAX = 10000
DRAIN_DEPTH = 8
# The starter's update: how long a new body runs healthy first, how many
# failed starts put the previous starter back
STARTER_AFTER = 3 * 24 * 3600
STARTER_TRIES = 3
# Answers, as the node bounds them (backend/host_channel.py)
DETAIL_MAX = 2000
REPORT_MAX = 4 * 1024 * 1024
LOG_TAIL = 400
LOG_MAX = 1024 * 1024                    # host/agent.log, one previous kept
STATE_MAX = 64 * 1024
VERIFY_MAX = 1024 * 1024
# The setup code a node logs at each start until its account exists
# (backend/device_auth._SETUP_CODE_FORM): never in a diagnostics report
SETUP_CODE = re.compile(r"[A-HJ-NP-Z2-9]{4}(?:-[A-HJ-NP-Z2-9]{4}){1}")

UNIT = "sautium-host-agent"
STARTER_NAME = "sautium-host-agent"

STARTER = """#!/bin/sh
# Sautium's host agent: the starter (interface {api}). Written by
# scripts/host_agent.py, which it runs from the checkout it sits in
# (<checkout>/host/bin/): never edit it by hand, `install` writes it again.
set -eu
root=$(dirname "$(dirname "$(dirname "$(readlink -f "$0")")")")
SAUTIUM_HOST_STARTER_API={api}
export SAUTIUM_HOST_STARTER_API
exec python3 -I "$root/scripts/host_agent.py" run
"""


def log(message: str) -> None:
    print(f"[host-agent] {time.strftime('%Y-%m-%d %H:%M:%S')} {message}", file=sys.stderr, flush=True)


class Refusal(Exception):
    """A verb that ends with an answer, not a crash."""

    def __init__(self, outcome: str, detail: str, release: Optional[str] = None):
        super().__init__(detail)
        self.outcome, self.detail, self.release = outcome, detail, release


# -- the agent's own folder ----------------------------------------------------

def host_dir(root: Path = None) -> Path:
    return (root or ROOT) / "host"


def _write_private(path: Path, data: bytes, mode: int = 0o600) -> None:
    """`path` written whole or not at all: a new file beside it, then one
    rename. Every path given here is under ./host/, which no container sees."""
    tmp = path.with_name(f".{path.name}.{os.getpid()}")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, mode)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise


def _read_bounded(path: Path, limit: int) -> Optional[bytes]:
    """A regular file's bytes, at most `limit`, without following a link;
    None when there is none."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
    except FileNotFoundError:
        return None
    with os.fdopen(fd, "rb") as f:
        if not stat.S_ISREG(os.fstat(f.fileno()).st_mode):
            raise OSError(errno.EINVAL, f"{path} is not a regular file")
        data = f.read(limit + 1)
    if len(data) > limit:
        raise OSError(errno.EFBIG, f"{path} is larger than {limit} bytes")
    return data


def load_state(root: Path = None) -> dict:
    raw = _read_bounded(host_dir(root) / "state.json", STATE_MAX)
    return json.loads(raw) if raw else {}


def save_state(state: dict, root: Path = None) -> None:
    _write_private(host_dir(root) / "state.json", json.dumps(state, indent=1, sort_keys=True).encode())


def read_env(path: Path) -> Dict[str, str]:
    """KEY=VALUE lines of a compose .env, as compose reads the simple form."""
    raw = _read_bounded(path, STATE_MAX)
    values = {}
    for line in (raw or b"").decode("utf-8", "replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        values[key.strip()] = value
    return values


def set_env(path: Path, key: str, value: str) -> None:
    """One key of .env set, every other line kept as it is; the file's mode
    kept (it holds the database password: the person's alone)."""
    raw = _read_bounded(path, STATE_MAX) or b""
    text = raw.decode("utf-8", "surrogateescape")
    lines = text.split("\n")[:-1] if text.endswith("\n") else text.split("\n") if text else []
    out, done = [], False
    for line in lines:
        if line.split("=", 1)[0].strip() == key and not line.lstrip().startswith("#"):
            if not done:
                out.append(f"{key}={value}")
                done = True
            continue
        out.append(line)
    if not done:
        out.append(f"{key}={value}")
    mode = stat.S_IMODE(os.stat(path).st_mode) if path.exists() else 0o600
    tmp = path.with_name(f".{path.name}.{os.getpid()}")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, mode)
    with os.fdopen(fd, "w", encoding="utf-8", errors="surrogateescape") as f:
        f.write("\n".join(out) + "\n")
        f.flush()
        os.fsync(f.fileno())
    os.chmod(tmp, mode)
    os.replace(tmp, path)


def channel_key(root: Path = None) -> bytes:
    """The channel's key, as host_channel.channel_key reads it in the node."""
    values = read_env(host_dir(root) / "agent.env")
    key = values.get("SAUTIUM_HOST_CHANNEL_KEY", "")
    if not re.fullmatch(r"[0-9a-f]{64}", key):
        raise SystemExit("[host-agent] no channel key in host/agent.env: run `install` first")
    return key.encode("ascii")


# -- the requests folder -------------------------------------------------------

def _open_dir(name: str, dir_fd: Optional[int]) -> int:
    return os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=dir_fd)


def _remove_tree(dir_fd: int, name: str, depth: int, budget: List[int]) -> bool:
    """Remove `name` under `dir_fd`, a folder bottom-up by handles: True once
    it is gone. Nothing is opened but folders, never through a link; a folder
    deeper than `depth` stays, and so does the rest once `budget` is spent."""
    if budget[0] <= 0:
        return False
    try:
        st = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
    except FileNotFoundError:
        return True
    if not stat.S_ISDIR(st.st_mode):
        budget[0] -= 1
        try:
            os.unlink(name, dir_fd=dir_fd)
        except FileNotFoundError:
            pass
        return True
    if depth <= 0:
        return False
    try:
        sub = _open_dir(name, dir_fd)
    except OSError as e:
        if e.errno in (errno.ELOOP, errno.ENOTDIR):        # swapped for something else meanwhile
            return _remove_tree(dir_fd, name, depth, budget)
        raise
    try:
        with os.scandir(sub) as entries:
            children = [entry.name for _, entry in zip(range(DRAIN_MAX), entries)]
        gone = all([_remove_tree(sub, child, depth - 1, budget) for child in children])
    finally:
        os.close(sub)
    if not gone:
        return False
    budget[0] -= 1
    try:
        os.rmdir(name, dir_fd=dir_fd)
    except FileNotFoundError:
        pass
    except OSError as e:
        if e.errno != errno.ENOTEMPTY:   # more children than one listing takes, or new ones meanwhile
            raise
        return False
    return True


def drain(root: Path = None) -> Tuple[List[str], int]:
    """(the verbs asked, entries left): the requests folder emptied by
    handles, every entry's name read and nothing else. A name of no verb is
    removed all the same. The folder is reached from the checkout by
    handles, each component a real folder: ./run itself is no container's,
    and ./run/requests is the bind's own mount point, which no container can
    swap; the walk holds even if one were."""
    root = root or ROOT
    top = _open_dir(str(root), None)
    try:
        run = _open_dir("run", top)
    finally:
        os.close(top)
    try:
        folder = _open_dir("requests", run)
    finally:
        os.close(run)
    asked, budget = set(), [DRAIN_MAX]
    try:
        with os.scandir(folder) as entries:          # names read up to the bound: memory stays bounded
            names = [entry.name for _, entry in zip(range(DRAIN_MAX), entries)]
        for name in names:
            if name in VERBS:
                asked.add(name)
            _remove_tree(folder, name, DRAIN_DEPTH, budget)
        with os.scandir(folder) as entries:
            left = sum(1 for _ in zip(range(DRAIN_MAX), entries))
    finally:
        os.close(folder)
    if left:
        log(f"{left} entr{'y' if left == 1 else 'ies'} of the requests folder could not be removed: "
            "the folder is not empty, so the path unit starts this again (and systemd fails it "
            "if that keeps on)")
    return [verb for verb in ORDER if verb in asked], left


# -- git, docker ---------------------------------------------------------------

def _run(args: List[str], *, cwd: Path = None, timeout: int = 120, stdin=None, stdout=None,
         check: bool = True, env: Optional[dict] = None) -> subprocess.CompletedProcess:
    result = subprocess.run(args, cwd=str(cwd or ROOT), timeout=timeout, stdin=stdin,
                            stdout=stdout if stdout is not None else subprocess.PIPE,
                            stderr=subprocess.PIPE, env=env)
    if check and result.returncode != 0:
        err = (result.stderr or b"").decode("utf-8", "replace").strip().splitlines()
        raise Refusal("failed", f"{args[0]} {args[1] if len(args) > 1 else ''} failed: "
                                f"{err[-1] if err else 'exit ' + str(result.returncode)}"[:DETAIL_MAX])
    return result


def git(*args: str, timeout: int = 120, root: Path = None) -> str:
    return _run(["git", *args], cwd=root, timeout=timeout).stdout.decode("utf-8", "replace").strip()


def compose_cmd(root: Path = None) -> List[str]:
    """`docker compose` on this node's files: the ones .env names in
    COMPOSE_FILE, which compose reads itself, as the person's own commands
    in the folder do — else the Linux file."""
    if read_env((root or ROOT) / ".env").get("COMPOSE_FILE"):
        return ["docker", "compose"]
    return ["docker", "compose", "-f", COMPOSE_FILE]


def compose(*args: str, timeout: int = 600, root: Path = None, check: bool = True) -> subprocess.CompletedProcess:
    return _run([*compose_cmd(root), *args], cwd=root, timeout=timeout, check=check)


def head(root: Path = None) -> str:
    return git("rev-parse", "--verify", "HEAD", root=root)


def image_of(root: Path = None) -> str:
    return read_env((root or ROOT) / ".env").get("SAUTIUM_BACKEND_IMAGE") or DEFAULT_IMAGE


def fetch_releases(root: Path = None) -> None:
    """Origin's release tags into SIGN-1's own namespace; nothing in the
    working tree moves."""
    git("fetch", "--quiet", "--no-tags", "--prune", "origin",
        f"+refs/tags/v*:{RELEASE_REFS}v*", timeout=1800, root=root)


# The verifier's one call, in a container of the installed image: the
# installed commit's desktop/ as a zip git made, its updater's walk over the
# fetched tags, no network, the checkout's .git read-only and nothing else of
# the live tree in sight (/app is not mounted: a planted module there would
# choose the "verified" commit). git runs in the git folder itself, which
# the person owns — a work tree Docker made for the mount would be root's,
# and git refuses a repository another user owns.
_VERIFY = """
import json, sys
sys.path.insert(0, "/verify/desktop.zip")
from desktop import updater
head = updater._rev("HEAD", "/repo.git")
found = updater.survey_fetched(updater.installed_signers("/repo.git"), head, "/repo.git")
def release(r):
    return None if r is None else {"name": r.name, "commit": r.commit}
print(json.dumps({"head": head, "installed": release(found.installed), "offered": release(found.offered),
                  "rewrite": release(found.rewrite), "refused": found.refused[:1], "fork": found.fork}))
"""


def survey(root: Path = None) -> dict:
    """Fetch, then judge with the installed tree's own verifier and signers."""
    root = root or ROOT
    fetch_releases(root)
    installed = head(root)
    verify = host_dir(root) / "verify"
    verify.mkdir(mode=0o700, exist_ok=True)
    archive = verify / "desktop.zip"
    tmp = verify / f".desktop.{os.getpid()}.zip"
    git("archive", "--format=zip", "-o", str(tmp), installed, "desktop", root=root)
    os.replace(tmp, archive)
    image = image_of(root)
    uid, gid = os.getuid(), os.getgid()
    result = _run(["docker", "run", "--rm", "--network", "none", "--read-only", "--tmpfs", "/tmp",
                   "--cap-drop", "ALL", "--security-opt", "no-new-privileges", "--no-healthcheck",
                   "--user", f"{uid}:{gid}", "--label", LABEL, "-e", "HOME=/tmp",
                   "-v", f"{root / '.git'}:/repo.git:ro", "-v", f"{archive}:/verify/desktop.zip:ro",
                   "-w", "/repo.git", "--entrypoint", "python3", image, "-I", "-c", _VERIFY],
                  cwd=root, timeout=600)
    out = result.stdout
    if len(out) > VERIFY_MAX:
        raise Refusal("failed", "the verifier said more than a survey")
    found = json.loads(out.decode("utf-8").strip().splitlines()[-1])
    if found.get("head") != installed:
        raise Refusal("failed", "the verifier judged another commit than the installed one")
    for key in ("installed", "offered", "rewrite"):
        r = found.get(key)
        if r is not None and not (re.fullmatch(r"[0-9a-f]{40}", r.get("commit", ""))
                                  and re.fullmatch(r"v[0-9]{1,6}\.[0-9]{1,6}\.[0-9]{1,6}", r.get("name", ""))):
            raise Refusal("failed", f"the verifier's {key} is not a release")
    return found


def _judged(found: dict) -> Tuple[str, str, Optional[str]]:
    """(outcome, detail, release) of a survey, as a check answers it."""
    if found["offered"]:
        name = found["offered"]["name"]
        return "offered", f"Sautium {name[1:]} is available", name
    if found["rewrite"]:
        name = found["rewrite"]["name"]
        return "rewrite", (f"Sautium {name[1:]} rewrites the project's history: a fresh install "
                           "moves to it"), name
    if found["fork"]:
        return "refused", f"two releases continue the same one apart ({', '.join(found['fork'])})", None
    if found["refused"]:
        name, why = found["refused"][0]
        return "refused", f"the release tag {name} is refused: {why}", None
    installed = found["installed"]["name"] if found["installed"] else None
    return "current", f"Sautium {installed[1:]} is the newest release" if installed else \
        "no signed release is newer than this checkout", installed


# -- the verbs -----------------------------------------------------------------

def verb_check(state: dict, now: float, root: Path = None) -> Tuple[str, str, Optional[str], Optional[str]]:
    last = state.get("check_at")
    if last is not None and now - last < CHECK_FLOOR:
        return "floor", f"checked {int(now - last)} s ago; at most once an hour", None, None
    state["check_at"] = now
    outcome, detail, release = _judged(survey(root))
    return outcome, detail, release, None


def _psql(root: Path, env: Dict[str, str], *statements: str, timeout: int = 3600) -> str:
    """Statements run one by one in this node's database container (by its
    compose project, never a container's global name: another node's could
    have it), on the maintenance database: a database is copied, dropped and
    renamed only from outside it. What the last one printed."""
    args = [*compose_cmd(root), "exec", "-T", "postgres", "psql", "-v", "ON_ERROR_STOP=1", "-At", "-U",
            env.get("POSTGRES_USER", "musicai"), "-d", "postgres"]
    for statement in statements:
        args += ["-c", statement]
    return _run(args, cwd=root, timeout=timeout).stdout.decode("utf-8", "replace").strip()


def _database(env: Dict[str, str]) -> str:
    name = env.get("POSTGRES_DB", "music_ai")
    if not re.fullmatch(r"[a-z_][a-z0-9_]{0,40}", name):
        raise Refusal("failed", f"the database's name {name!r} is not one the agent copies")
    return name


def _databases(root: Path, env: Dict[str, str]) -> List[str]:
    return _psql(root, env, "SELECT datname FROM pg_database").split()


def _close_sessions(name: str) -> str:
    return (f"SELECT count(pg_terminate_backend(pid)) FROM pg_stat_activity "
            f"WHERE datname = '{name}' AND pid <> pg_backend_pid()")


def _database_up(root: Path) -> bool:
    """The database's container up and healthy: after a power cut the agent
    may start before Docker has, or while PostgreSQL recovers."""
    return compose("up", "-d", "--wait", "--wait-timeout", "600", "postgres", root=root, timeout=700,
                   check=False).returncode == 0


def _clear_copies(root: Path, env: Dict[str, str]) -> None:
    """What earlier updates left beside the database, dropped before a new
    one: the copy a cut run made, the database a failed release left (kept
    until now, so nothing it wrote was lost unasked)."""
    name = _database(env)
    _psql(root, env, *[f'DROP DATABASE IF EXISTS "{name}_{kind}" WITH (FORCE)'
                       for kind in ("pre_update", "failed_update")])


def _room_for_copy(root: Path, env: Dict[str, str]) -> None:
    """The copy needs the database's size again on the volume, with room to
    spare: a full disk would stop PostgreSQL, the node's and the way back's.
    Asked after the build, whose image lands on the same disk."""
    name = _database(env)
    size = int(_psql(root, env, f"SELECT pg_database_size('{name}')"))
    free = _run([*compose_cmd(root), "exec", "-T", "postgres", "df", "-B1", "--output=avail",
                 "/var/lib/postgresql"], cwd=root).stdout.decode().split()[-1]
    if int(free) < size * 1.2 + (1 << 30):
        raise Refusal("refused", f"the database ({size >> 20} MB) has no room for its copy beside it "
                                 f"({int(free) >> 20} MB free): nothing moved")


def _snapshot(root: Path, env: Dict[str, str]) -> str:
    """The database before the move, whole — the dump loaders' tables
    included — as a copy beside it in the database's own container, made
    while the backend is stopped and dropped once the new node is up. The way
    back renames it into place: exact, where a restore of a dump into the
    live database is not (on the Pi a clean restore failed twice on what the
    tables left out of it hold on the rest — a key, a function their indexes
    use), and nothing of it passes through the host."""
    name = _database(env)
    copy = f"{name}_pre_update"
    _psql(root, env, f'DROP DATABASE IF EXISTS "{copy}"', _close_sessions(name),
          f'CREATE DATABASE "{copy}" TEMPLATE "{name}" STRATEGY FILE_COPY')
    return copy


def _restore(root: Path, env: Dict[str, str], copy: str) -> None:
    """The copy renamed into place, the live database renamed away to
    `<db>_failed_update` and KEPT (the next update drops it): at every step
    one of the two is whole under a name a next try finds, and nothing the
    failed release wrote is lost unasked."""
    name = _database(env)
    held = _databases(root, env)
    if copy not in held:
        if name in held:
            return                       # put back already, by a way back cut short after its renames
        raise Refusal("failed", f"neither {name} nor its copy {copy} is in the database server")
    failed = f"{name}_failed_update"
    steps = [f'DROP DATABASE IF EXISTS "{failed}" WITH (FORCE)']
    if name in held:
        steps += [_close_sessions(name), f'ALTER DATABASE "{name}" RENAME TO "{failed}"']
    steps += [f'ALTER DATABASE "{copy}" RENAME TO "{name}"']
    _psql(root, env, *steps)


def _healthy(root: Path) -> bool:
    """The new node up and answering its health check, by Docker's own wait
    (the Linux file gives a start half an hour before a failing check
    counts: its migrations run before it serves)."""
    result = compose("up", "-d", "--no-build", "--wait", "--wait-timeout", "2400", root=root,
                     timeout=2500, check=False)
    if result.returncode != 0:
        said = (result.stderr or b"").decode("utf-8", "replace").strip().splitlines()
        log(f"the node did not come up healthy: {' / '.join(said[-3:]) or 'exit ' + str(result.returncode)}")
    return result.returncode == 0


def _body_answers(root: Path) -> bool:
    """The checkout's body starts and speaks the starter's interface: a
    release whose agent cannot run would leave the node with no way to ask
    for the next one, this update's way back included."""
    env = {**os.environ, "SAUTIUM_HOST_STARTER_API": str(STARTER_API)}
    result = _run([sys.executable, "-I", str(root / "scripts" / "host_agent.py"), "selftest"], cwd=root,
                  env=env, check=False, timeout=60)
    if result.returncode != 0:
        log(f"the release's host agent does not start: {(result.stderr or b'').decode('utf-8', 'replace')[-300:]}")
    return result.returncode == 0


def _front_restarted(root: Path, old: str, new: str) -> Optional[str]:
    """On a master — the machine whose owner installed the trusted front's
    user unit, `sautium-front.service` (scripts/master-front/ holds what it
    runs) — the front comes back with an update that changed it. The agent
    never reads the node's identity: it is in a folder the node writes."""
    if _run(["systemctl", "--user", "cat", "sautium-front.service"], cwd=root, check=False).returncode != 0:
        return None
    if not git("diff", "--name-only", old, new, "--", "scripts/master-front/", root=root):
        return None
    done = _run(["systemctl", "--user", "restart", "sautium-front.service"], cwd=root, check=False)
    return "the trusted front restarted" if done.returncode == 0 else "the trusted front did not restart: see its unit"


# What may lie in the code's folders beside what git tracks: the node's own
# data folder (its bind), bytecode caches no image interpreter reads
# (PYTHONPYCACHEPREFIX), what the host puts there itself (the atlas's file),
# and the MCP config earlier releases wrote. Anything else was left by a node
# from before its code was read-only — a backend/.env Settings would read, a
# module named like the standard library's, a bytecode file without its
# source — and would run in every release after this one. The providers
# folder is code the node imports at every start: each package in it must be
# one the person named in host/allowed-providers (a file no container sees).
LEFT_ALLOWED = re.compile(r"(backend/data/|.*/__pycache__/|.*\.pyc|backend/hqp_atlas\.json|"
                          r"backend/hqp_atlas\.version\.json|backend/mcp-docker\.json)")
PROVIDERS = "backend/streaming/providers"


def _allowed_providers(root: Path) -> set:
    raw = _read_bounded(host_dir(root) / "allowed-providers", STATE_MAX) or b""
    return {line.strip() for line in raw.decode("utf-8", "replace").splitlines()
            if line.strip() and not line.startswith("#")}


def leftovers(root: Path = None) -> List[str]:
    """Untracked or ignored paths in the code's folders that LEFT_ALLOWED does
    not name, and provider packages the person did not name. A bytecode file
    is allowed only inside __pycache__/: one beside its folder's sources is
    imported even with no source."""
    root = root or ROOT
    out = _run(["git", "status", "--porcelain=v1", "-z", "--ignored=matching", "--untracked-files=all",
                "--", "backend", "mcp"], cwd=root).stdout.decode("utf-8", "surrogateescape")
    found = []
    for entry in out.split("\0"):
        if entry[:3] not in ("?? ", "!! "):
            continue
        path = entry[3:]
        if path.rstrip("/") == PROVIDERS or path.startswith(PROVIDERS + "/"):
            continue                     # judged by name below
        sourceless = path.endswith(".pyc") and "/__pycache__/" not in path
        if sourceless or not LEFT_ALLOWED.fullmatch(path):
            found.append(path)
    providers, allowed = root / PROVIDERS, _allowed_providers(root)
    try:
        with os.scandir(providers) as entries:
            for entry in entries:
                if entry.name == "__pycache__" and entry.is_dir(follow_symlinks=False):
                    continue
                tracked = _run(["git", "ls-files", "--error-unmatch", "--", f"{PROVIDERS}/{entry.name}"],
                               cwd=root, check=False).returncode == 0
                if tracked:
                    continue
                if entry.name not in allowed or entry.is_symlink():
                    found.append(f"{PROVIDERS}/{entry.name}")
    except FileNotFoundError:
        pass
    return found


def _way_back(root: Path, env: Dict[str, str], moving: dict) -> Tuple[str, str, bool]:
    """The release before, its image and its database put back, and the node
    started: each step tried whatever the one before did — a node left down
    answers nobody. (outcome, what happened, whether the database is back)."""
    problems, restored = [], not moving.get("copy")

    def step(what: str, do) -> bool:
        try:
            do()
            return True
        except (Refusal, OSError, ValueError, subprocess.SubprocessError) as e:
            problems.append(f"{what}: {getattr(e, 'detail', e)}")
            log(f"the way back could not {what}: {getattr(e, 'detail', e)}")
            return False

    step("stop the node", lambda: compose("stop", "backend", root=root, check=False))
    step("check the release before out", lambda: git("-c", "advice.detachedHead=false", "checkout", "--quiet",
                                                       "--force", "--detach", moving["from"], root=root))
    step("name its image", lambda: set_env(root / ".env", "SAUTIUM_BACKEND_IMAGE", moving["image"]))
    step("remove the release's container", lambda: compose("rm", "-f", "backend", root=root, check=False))
    if moving.get("tag") and moving["tag"] != moving["image"]:
        step("remove the release's image", lambda: _run(["docker", "image", "rm", moving["tag"]], cwd=root,
                                                         check=False))
    if moving.get("copy"):
        restored = step("put the database back", lambda: _restore(root, env, moving["copy"]))
    if not _healthy(root):
        return "failed", ("the release before it did not start again after the way back"
                          + (f" ({'; '.join(problems)})" if problems else "") + ": see the node's log"), restored
    if problems:
        return "failed", f"the node is back on the release before it, but: {'; '.join(problems)}", restored
    return "undone", ("the node is back on the release before it"
                      + (", its database as it was before the update (the one the release left is kept "
                         "beside it until the next update)" if moving.get("copy") else "")), restored


def _finished(state: dict, root: Path, env: Dict[str, str], moving: dict, now: float) -> str:
    """The release kept: the state saved first — a cut after this point
    leaves nothing to undo — then the copy and the image before dropped."""
    former = (state.get("installed") or {}).get("previous")
    state["installed"] = {"name": moving["name"], "commit": moving["to"], "at": now, "image": moving["tag"],
                          "previous": moving["image"], "runs_ok": 0}
    for key in ("update_noop_at", "update_held_at", "failed_since_installed", "update_failed", "moving"):
        state.pop(key, None)
    save_state(state, root)
    try:
        _psql(root, env, f'DROP DATABASE IF EXISTS "{moving["copy"]}"')
    except Refusal as e:
        log(f"the database's copy stays beside it until the next update: {e.detail}")
    if former and former not in (moving["tag"], moving["image"]) and former.startswith("sautium-backend:v"):
        _run(["docker", "image", "rm", former], cwd=root, check=False)
    front = _front_restarted(root, moving["from"], moving["to"])
    return f"moved to Sautium {moving['name'][1:]}" + (f"; {front}" if front else "")


def _go_back(state: dict, root: Path, env: Dict[str, str], moving: dict, now: float,
             why: str) -> Tuple[str, str, Optional[str], Optional[str]]:
    log(f"{moving['name']} {why}: the way back to {moving['from'][:12]}")
    outcome, back, restored = _way_back(root, env, moving)
    state["update_failed"] = {"commit": moving["to"], "at": now}
    if restored:
        state.pop("moving", None)
    else:
        # The database is not back: the next start of the agent tries again
        # (not sooner than RETRY_BACK), never leaving it on the new schema
        moving["back_tried_at"] = now
        moving["from_only"] = True
    return outcome, f"Sautium {moving['name'][1:]} {why}: {back}", moving["name"], None


def verb_update(state: dict, now: float, root: Path = None) -> Tuple[str, str, Optional[str], Optional[str]]:
    """C4's update, unattended once the person asked: verify, build from a
    tree git made of the verified commit, stop, copy the database, move,
    start; the way back when the new node or its agent does not come up, and
    whatever stops the update after the node stopped. A release that did not
    start or build here is not tried again for a day, nor is an update that
    found nothing to do."""
    root = root or ROOT
    if state.get("moving"):
        return "floor", "an update is still being put back; the agent tries again at its next start", None, None
    noop = state.get("update_noop_at")
    if noop is not None and now - noop < UPDATE_NOOP_FLOOR:
        return "floor", "an update found nothing to do less than a day ago", None, None
    held = state.get("update_held_at")
    if held is not None and now - held < HELD_FLOOR:
        return "floor", "an update was refused for the checkout's own files minutes ago", None, None
    found = survey(root)
    state["check_at"] = now
    if not found["offered"]:
        outcome, detail, release = _judged(found)
        state["update_noop_at"] = now
        return ("current" if outcome == "current" else "refused"), detail, release, None
    name, commit = found["offered"]["name"], found["offered"]["commit"]
    tried = state.get("update_failed") or {}
    if tried.get("commit") == commit and now - tried["at"] < UPDATE_NOOP_FLOOR:
        return "floor", f"Sautium {name[1:]} did not start here less than a day ago", name, None
    if git("status", "--porcelain", "--untracked-files=no", root=root):
        state["update_held_at"] = now
        return "refused", "the checkout has local changes: an update never moves a tree someone edits", name, None
    left = leftovers(root)
    if left:
        state["update_held_at"] = now
        return "refused", ("files git does not know of lie among the code, and would run in every release: "
                           + ", ".join(left[:5]) + (" …" if len(left) > 5 else "")), name, None
    env = read_env(root / ".env")
    try:
        carried = build_args(env)
    except ValueError as e:
        state["update_held_at"] = now
        return "refused", f"{e}: an update builds what .env says the image carries", name, None
    tag = f"sautium-backend:{name}-{commit[:12]}"
    log(f"building {tag} from {name} ({commit})")
    # A build is the heaviest thing a forged request asks for: once a day,
    # whatever ends it
    state["update_failed"] = {"commit": commit, "at": now}
    save_state(state, root)
    archive = subprocess.Popen(["git", "archive", "--format=tar", f"{commit}:backend"], cwd=str(root),
                               stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    try:
        _run(["docker", "build", "-f", DOCKERFILE, *carried, "-t", tag, "-"], cwd=root,
             stdin=archive.stdout, timeout=3 * 3600)
    except Exception as e:
        raise Refusal("failed", f"the image of {name} did not build "
                                f"({getattr(e, 'detail', None) or type(e).__name__}): nothing moved", name) from e
    finally:
        archive.stdout.close()
        archive.wait()
    if archive.returncode != 0:
        return "failed", f"git archive of {name} failed: nothing moved", name, None
    _clear_copies(root, env)
    _room_for_copy(root, env)
    # Kept before the node stops: a run that dies past this point (power,
    # a kill) is finished or undone by the next start of the agent
    moving = state["moving"] = {"from": head(root), "image": image_of(root), "to": commit, "name": name,
                                "tag": tag, "copy": None}
    save_state(state, root)
    healthy, why = False, "did not come up healthy"
    try:
        log("stopping the node, then the copy of its database")
        compose("stop", "backend", root=root)
        moving["copy"] = _snapshot(root, env)
        save_state(state, root)
        log(f"moving the checkout to {name} and starting it")
        git("-c", "advice.detachedHead=false", "checkout", "--quiet", "--detach", commit, root=root)
        set_env(root / ".env", "SAUTIUM_BACKEND_IMAGE", tag)
        healthy = _healthy(root)
        if healthy and not _body_answers(root):
            healthy, why = False, "came up, but its host agent does not start"
    except Exception as e:                # whatever stopped it, the way back below runs
        why = f"could not be put in place ({getattr(e, 'detail', None) or f'{type(e).__name__}: {e}'})"
        log(f"the update stopped: {why}")
    if healthy:
        return "updated", _finished(state, root, env, moving, now), name, None
    return _go_back(state, root, env, moving, now, why)


def build_args(env: Dict[str, str]) -> List[str]:
    """`--build-arg` for each choice .env records of what the image carries; a
    key it does not hold is the Dockerfile's own default."""
    args = []
    for arg, key in IMAGE_ARGS:
        value = env.get(key, "")
        if value not in ("", "0", "1"):
            raise ValueError(f"{key} in .env is neither 0 nor 1")
        if value:
            args += ["--build-arg", f"{arg}={value}"]
    return args


class NotReady(Exception):
    """Docker or the database is not up yet: the agent's boot unit starts the
    body again shortly."""


def resume(state: dict, now: float, root: Path = None) -> Optional[Tuple[str, str, Optional[str], Optional[str]]]:
    """An update a run did not finish — the machine lost power, the agent was
    killed, the unit's time ran out — finished at the next start of the agent
    (the boot unit starts it at boot too): kept when the checkout had moved
    and the release is up and its agent answers, else taken back. Its answer,
    or None. Nothing is touched until the database is up."""
    moving = state.get("moving")
    if not moving:
        return None
    root = root or ROOT
    if moving.get("back_tried_at") is not None and now - moving["back_tried_at"] < RETRY_BACK:
        return None
    if not _database_up(root):
        raise NotReady("the database's container is not up")
    env = read_env(root / ".env")
    moved = (not moving.get("from_only") and image_of(root) == moving["tag"]
             and git("rev-parse", "--verify", "HEAD", root=root) == moving["to"])
    if moved and _healthy(root) and _body_answers(root):
        log(f"an update to {moving['name']} did not finish its run, and the release is up: kept")
        return "updated", "the update finished at the agent's next start: " + \
            _finished(state, root, env, moving, now), moving["name"], None
    return _go_back(state, root, env, moving, now, "did not finish")


def _scrub(text: str) -> str:
    return SETUP_CODE.sub("XXXX-XXXX", text)


def verb_diagnostics(state: dict, now: float, root: Path = None) -> Tuple[str, str, Optional[str], Optional[str]]:
    """F2's table: what the two containers logged, the power, the disk, the
    versions — no journal, no kernel log, no file listing."""
    root = root or ROOT
    last = state.get("diagnostics_at")
    if last is not None and now - last < DIAGNOSTICS_FLOOR:
        return "floor", f"made {int(now - last)} s ago; at most once in ten minutes", None, None
    state["diagnostics_at"] = now
    parts = []

    def section(title: str, args: List[str], timeout: int = 60) -> None:
        try:
            r = _run(args, cwd=root, timeout=timeout, check=False)
            body = (r.stdout or b"") + (r.stderr or b"")
            parts.append(f"== {title}\n{body.decode('utf-8', 'replace').rstrip()}\n")
        except (OSError, subprocess.TimeoutExpired) as e:
            parts.append(f"== {title}\n(not read: {e})\n")

    section("versions", ["sh", "-c", "uname -srm; docker version --format "
                                     "'docker {{.Server.Version}}'; docker compose version --short; "
                                     ". /etc/os-release && echo \"$PRETTY_NAME\""])
    section("power", ["sh", "-c", "command -v vcgencmd >/dev/null && vcgencmd get_throttled "
                                  "|| echo 'no vcgencmd here'"])
    section("disk", ["sh", "-c", f"df -h {shlex.quote(str(root))} \"$(docker info --format "
                                 "'{{.DockerRootDir}}')\""])
    section("containers", [*compose_cmd(root), "ps", "--all"])
    for service in ("backend", "postgres"):
        section(f"log: {service}", [*compose_cmd(root), "logs", "--no-color", "--tail", str(LOG_TAIL), service])
    section("log: the host agent", ["tail", "-n", str(LOG_TAIL), str(host_dir(root) / "agent.log")])
    report = _scrub("".join(parts))
    if len(report.encode()) > REPORT_MAX:
        report = report.encode()[-REPORT_MAX:].decode("utf-8", "ignore")
    return "done", "diagnostics made on the machine", None, report


VERB = {"check": verb_check, "update": verb_update, "diagnostics": verb_diagnostics}


# -- the answer ----------------------------------------------------------------

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def answer(verb: str, outcome: str, detail: str, release: Optional[str], report: Optional[str],
           key: bytes, api: str = NODE_API) -> bool:
    """The answer through the node's API, signed as auth_hmac's canonical
    string has it with the channel's key: False when the node did not take it
    (logged). The node may be hostile: no redirect, ten seconds, its body
    unread."""
    body = json.dumps({"verb": verb, "outcome": outcome, "detail": detail[:DETAIL_MAX],
                       "release": release, "report": report}).encode()
    ts, nonce = str(int(time.time())), secrets.token_hex(16)
    digest = hashlib.sha256(body).hexdigest()
    sig = hmac.new(key, f"POST\n{ANSWER_PATH}\n{ts}\n{nonce}\n{digest}".encode(), hashlib.sha256).hexdigest()
    request = urllib.request.Request(api + ANSWER_PATH, data=body, method="POST", headers={
        "Content-Type": "application/json", "X-Sautium-Ts": ts, "X-Sautium-Nonce": nonce,
        "X-Sautium-Body-Sha256": digest, "X-Sautium-Sig": sig})
    opener = urllib.request.build_opener(_NoRedirect, urllib.request.ProxyHandler({}))

    def late(signum, frame):
        raise TimeoutError("no answer within 20 s")

    # The socket's own timeout bounds each read, not the whole: a node that
    # sends a byte every few seconds would hold the agent for hours
    before = signal.signal(signal.SIGALRM, late)
    signal.alarm(20)
    try:
        with opener.open(request, timeout=10) as response:
            return 200 <= response.status < 300
    except (urllib.error.URLError, OSError) as e:
        log(f"the node did not take the answer to {verb}: {e}")
        return False
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, before)


# -- the starter ---------------------------------------------------------------

def starter_text(api: int = STARTER_API) -> bytes:
    return STARTER.format(api=api).encode()


def _starter_paths(root: Path = None) -> Tuple[Path, Path, Path]:
    bin_dir = host_dir(root) / "bin"
    return bin_dir / STARTER_NAME, bin_dir / f"{STARTER_NAME}.previous", bin_dir / "starter-trial"


def maintain_starter(state: dict, now: float, root: Path = None, start_probe=None) -> Optional[str]:
    """The starter's own update, under PI5-PLAN's four conditions; what
    happened, or None. A trial in progress ends here: the starter that ran
    this run is the new one, so it is kept. One the restore unit put back
    is never tried again."""
    current, previous, trial = _starter_paths(root)
    if trial.exists():
        trial.unlink()
        state.pop("starter_trying", None)
        state["starter_since"] = now
        return "the new starter ran: kept"
    wanted = starter_text()
    digest = hashlib.sha256(wanted).hexdigest()
    if state.get("starter_trying"):
        state["starter_refused"] = state.pop("starter_trying")
        return "the new starter failed its starts and the previous one is back: it is not tried again"
    installed = state.get("installed")
    if (not installed or now - installed["at"] < STARTER_AFTER or state.get("failed_since_installed")
            or installed.get("runs_ok", 0) < 1 or state.get("starter_refused") == digest):
        return None
    try:
        held = _read_bounded(current, STATE_MAX)
    except OSError:
        held = None
    if held == wanted or held is None or installed["commit"] != head(root):
        return None             # nothing to change, or HEAD is no release the agent verified and moved to
    _write_private(previous, held, 0o755)
    _write_private(trial, b"0\n", 0o600)
    state["starter_trying"] = digest
    _write_private(current, wanted, 0o755)
    (start_probe or _start_probe)()
    log("a new starter is in place; the probe tries it, and the previous one comes back "
        f"after {STARTER_TRIES} failed starts")
    return "a new starter is on trial"


def _start_probe() -> None:
    _run(["systemctl", "--user", "start", "--no-block", f"{UNIT}-probe.service"], check=False)


# -- a run ---------------------------------------------------------------------

def run(root: Path = None, api: str = NODE_API, now: Optional[float] = None,
        verbs: Optional[dict] = None, start_probe=None) -> int:
    """One start of the service: an update a run did not finish undone, the
    requests drained, each asked verb run and answered, the starter looked
    after. 0 unless the body itself broke — a verb's failure is an answer,
    not a failed start."""
    root = root or ROOT
    api_version = os.environ.get("SAUTIUM_HOST_STARTER_API", "")
    if not api_version.isdigit() or int(api_version) < 1:
        log("not started by a starter (no SAUTIUM_HOST_STARTER_API): refused")
        return 2
    key = channel_key(root)
    agent_log = host_dir(root) / "agent.log"
    if agent_log.is_file() and agent_log.stat().st_size > LOG_MAX:
        os.replace(agent_log, agent_log.with_name("agent.log.1"))   # this run writes on in the old one
    lock = os.open(host_dir(root) / "agent.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        fcntl.flock(lock, fcntl.LOCK_EX)
        state = load_state(root)
        failed = False

        def said(verb: str, outcome: str, detail: str, release: Optional[str], report: Optional[str]) -> None:
            nonlocal failed
            failed = failed or outcome == "failed"
            log(f"{verb}: {outcome} — {detail}")
            unsent = state.setdefault("unsent", {})
            if answer(verb, outcome, detail, release, report, key, api):
                unsent.pop(verb, None)
            else:
                # The node was down (an update that could not start it): the
                # next run, whatever its request, says it first. The last
                # answer of each verb only.
                unsent[verb] = [outcome, detail, release]
            if not unsent:
                state.pop("unsent")
            save_state(state, root)

        for verb, (outcome, detail, release) in list((state.get("unsent") or {}).items()):
            said(verb, outcome, detail, release, None)
        asked, _left = drain(root)
        try:
            resumed = resume(state, time.time() if now is None else now, root)
        except NotReady as e:
            # Nothing touched; a failed start, which the boot unit retries.
            # What was asked meanwhile is dropped with the node down: it asks again
            log(f"an unfinished update waits: {e}")
            save_state(state, root)
            return 3
        if resumed:
            said("update", *resumed)
        for verb in asked:
            moment = time.time() if now is None else now
            try:
                outcome, detail, release, report = (verbs or VERB)[verb](state, moment, root)
            except Refusal as r:
                outcome, detail, release, report = r.outcome, r.detail, r.release, None
            except Exception as e:        # the verb's failure is its answer; the log keeps the trace
                log(traceback.format_exc().rstrip())
                outcome, detail, release, report = "failed", f"{type(e).__name__}: {e}", None, None
            said(verb, outcome, detail, release, report)
        installed = state.get("installed")
        if failed:
            state["failed_since_installed"] = True
        elif installed and asked:
            installed["runs_ok"] = installed.get("runs_ok", 0) + 1
        note = maintain_starter(state, time.time() if now is None else now, root, start_probe)
        if note:
            log(note)
        save_state(state, root)
    finally:
        os.close(lock)
    return 0


def selftest(root: Path = None) -> int:
    """What an update asks of a release's body before it keeps the release:
    it starts, under a starter's interface, and reads its own folder."""
    root = root or ROOT
    api_version = os.environ.get("SAUTIUM_HOST_STARTER_API", "")
    if not api_version.isdigit() or int(api_version) < 1:
        return 2
    channel_key(root)
    load_state(root)
    units(root)
    return 0


# -- install -------------------------------------------------------------------

_UNSAFE_PATH = re.compile(r"[^A-Za-z0-9._/+-]")


def units(root: Path) -> Dict[str, str]:
    """The user units, for a checkout at `root` (a path systemd needs no
    escaping for). The restore unit's shell is written here once and never
    by a body: it is the floor of the starter's update."""
    bin_dir = host_dir(root) / "bin"
    starter = bin_dir / STARTER_NAME
    restore = (
        f"t={bin_dir}/starter-trial; [ -f \"$t\" ] || exit 0; "
        f"n=$(( $(cat \"$t\") + 1 )); "
        f"if [ \"$n\" -ge {STARTER_TRIES} ]; then "
        f"mv -f {bin_dir}/{STARTER_NAME}.previous {starter} && rm -f \"$t\"; "
        f"else echo \"$n\" > \"$t\"; fi; "
        f"systemctl --user start --no-block {UNIT}-probe.service")
    # The agent's own log, beside its files: a person's user journal is not
    # always kept, nor readable by them
    log_file = host_dir(root) / "agent.log"
    # A start's steps are bounded each (the build 3 h, each database step an
    # hour, the waits for health 40 min): the unit's bound is above their sum
    # at worst, about 13 h, so a run ends by its own steps
    service = (f"[Unit]\nDescription=Sautium host agent: %s\nOnFailure={UNIT}-restore.service\n%s\n"
               f"[Service]\nType=oneshot\nExecStart={starter}\nTimeoutStartSec=14h\n"
               f"StandardOutput=append:{log_file}\nStandardError=append:{log_file}\n"
               "NoNewPrivileges=yes\n%s")
    return {
        f"{UNIT}.path": (f"[Unit]\nDescription=Sautium host agent: the node's requests\n\n"
                         f"[Path]\nDirectoryNotEmpty={root}/run/requests\nUnit={UNIT}.service\n\n"
                         "[Install]\nWantedBy=default.target\n"),
        f"{UNIT}.service": service % ("a request from the node", "", ""),
        f"{UNIT}-probe.service": service % ("a new starter tried", "", ""),
        # At the user manager's start — at boot, with lingering on — so an
        # update the machine's power cut short is finished or undone with no
        # request; started again until Docker and the database are up
        f"{UNIT}-boot.service": service % ("an unfinished update, at the start", "StartLimitIntervalSec=0\n",
                                           "Restart=on-failure\nRestartSec=30s\n\n"
                                           "[Install]\nWantedBy=default.target\n"),
        f"{UNIT}-restore.service": (f"[Unit]\nDescription=Sautium host agent: the previous starter back "
                                    f"after {STARTER_TRIES} failed starts\n\n"
                                    f"[Service]\nType=oneshot\nExecStart=/bin/sh -c '{restore}'\n"),
    }


def unit_dir() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "systemd" / "user"


def install(root: Path = None, systemctl=None) -> int:
    """The agent for this checkout, as the person: its folder, the channel's
    key, the requests folder, the starter, the units. Run again, it writes
    the starter and the units again and keeps the key."""
    root = root or ROOT
    if os.geteuid() == 0:
        raise SystemExit("[host-agent] run install as the person who owns the checkout, not as root")
    if _UNSAFE_PATH.search(str(root)):
        raise SystemExit(f"[host-agent] the checkout's path {root} has a character the units cannot "
                         "carry: put the checkout at a plain path (letters, digits, . _ - /)")
    if root.stat().st_uid != os.getuid():
        raise SystemExit(f"[host-agent] {root} is not this user's: run install as its owner")
    left = leftovers(root)
    if left:
        raise SystemExit("[host-agent] files git does not know of lie among the code (a node from before its "
                         "code was read-only could have left them, and they would run in every release): "
                         + ", ".join(left[:20]) + " — remove them, or check them out again from git")
    host = host_dir(root)
    host.mkdir(mode=0o700, exist_ok=True)
    (host / "bin").mkdir(mode=0o700, exist_ok=True)
    for path in (host, host / "bin"):
        st = os.lstat(path)
        if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid():
            raise SystemExit(f"[host-agent] {path} is not this user's own folder")
    (root / "run").mkdir(mode=0o755, exist_ok=True)
    requests = root / "run" / "requests"
    requests.mkdir(mode=0o700, exist_ok=True)
    st = os.lstat(requests)
    if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid():
        raise SystemExit(f"[host-agent] {requests} is not this user's own folder (Docker makes it "
                         "root's when the node starts before the agent is installed, and the node "
                         "hands it over at its next start): `docker compose up -d`, then install again")
    env = host / "agent.env"
    if read_env(env).get("SAUTIUM_HOST_CHANNEL_KEY") is None:
        _write_private(env, f"SAUTIUM_HOST_CHANNEL_KEY={secrets.token_hex(32)}\n".encode())
    current, previous, trial = _starter_paths(root)
    try:
        held = _read_bounded(current, STATE_MAX)
    except OSError:
        held = None
    if held is not None and held != starter_text():
        _write_private(previous, held, 0o755)
    _write_private(current, starter_text(), 0o755)
    trial.unlink(missing_ok=True)
    folder = unit_dir()
    folder.mkdir(parents=True, exist_ok=True)
    for name, text in units(root).items():
        _write_private(folder / name, text.encode(), 0o644)
    ctl = systemctl or (lambda *args: _run(["systemctl", "--user", *args], cwd=root))
    ctl("daemon-reload")
    ctl("enable", f"{UNIT}-boot.service")
    ctl("enable", "--now", f"{UNIT}.path")
    print(f"The host agent is installed for {root}.")
    user = os.environ.get("USER") or str(os.getuid())
    linger = _run(["loginctl", "show-user", user, "-p", "Linger", "--value"], cwd=root, check=False)
    if linger.stdout.decode().strip() != "yes":
        print("It runs while you are logged in. To keep it running with nobody logged in, the "
              f"machine's administrator runs once:\n    sudo loginctl enable-linger {user}")
    print("The node learns of the agent when it is made again: docker compose up -d")
    return 0


def uninstall(root: Path = None, systemctl=None) -> int:
    """The units removed, and the channel's key: the node made again then
    knows it has no agent. ./host/ keeps the agent's log and state."""
    root = root or ROOT
    ctl = systemctl or (lambda *args: _run(["systemctl", "--user", *args], check=False))
    ctl("disable", "--now", f"{UNIT}.path")
    ctl("disable", f"{UNIT}-boot.service")
    folder = unit_dir()
    for name in units(root):
        (folder / name).unlink(missing_ok=True)
    ctl("daemon-reload")
    (host_dir(root) / "agent.env").unlink(missing_ok=True)
    print("The host agent's units and key are removed. The node learns of it when it is made again: "
          "docker compose up -d")
    return 0


def status(root: Path = None) -> int:
    root = root or ROOT
    state = load_state(root)
    print(json.dumps({"body": BODY_VERSION, "starter_api": STARTER_API, "state": state}, indent=1))
    _run(["systemctl", "--user", "--no-pager", "status", f"{UNIT}.path"], cwd=root, check=False,
         stdout=sys.stdout.buffer)
    return 0


def main(argv: List[str]) -> int:
    parser = argparse.ArgumentParser(prog="host_agent.py", description=__doc__.splitlines()[0])
    parser.add_argument("command", choices=("run", "install", "uninstall", "status", "selftest"))
    args = parser.parse_args(argv)
    if args.command == "run":
        return run()
    if args.command == "selftest":
        return selftest()
    if args.command == "install":
        return install()
    if args.command == "uninstall":
        return uninstall()
    return status()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
