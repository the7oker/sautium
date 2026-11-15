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
import stat
import subprocess
import sys
import time
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
SETUP_CODE = re.compile(r"[A-HJ-NP-Z2-9]{4}(?:-[A-HJ-NP-Z2-9]{4}){2}")

# The tables the dump loaders fill (backend/mb_dump_load.TABLES,
# lb_dump_load.TABLES; a test holds them together): tens of GB a loader makes
# again, left out of the pre-update dump whole and so left as they are by its
# restore. Never a pattern: `mb_*` also names tables of the node's own
# (mb_refuted_bindings, lb_slice_requests) whose keys point into the
# catalogue, and a restore that cleans the catalogue cannot drop a key those
# hold on it.
DUMP_TABLES = (
    "mb_area", "mb_release_group_primary_type", "mb_release_group_secondary_type", "mb_artist",
    "mb_artist_alias", "mb_artist_credit", "mb_artist_credit_name", "mb_release_group",
    "mb_release_group_secondary_type_join", "mb_release", "mb_release_country", "mb_release_unknown_country",
    "mb_medium_format", "mb_medium", "mb_recording", "mb_track", "mb_release_label", "mb_url",
    "mb_l_artist_url", "mb_l_release_url", "mb_tag", "mb_artist_tag", "mb_release_group_tag",
    "lb_recording", "lb_artist",
)

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
    lines = raw.decode("utf-8").splitlines()
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
    with os.fdopen(fd, "w", encoding="utf-8") as f:
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
        gone = all([_remove_tree(sub, child, depth - 1, budget) for child in os.listdir(sub)])
    finally:
        os.close(sub)
    if not gone:
        return False
    budget[0] -= 1
    try:
        os.rmdir(name, dir_fd=dir_fd)
    except FileNotFoundError:
        pass
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
        for name in os.listdir(folder):
            if name in VERBS:
                asked.add(name)
            _remove_tree(folder, name, DRAIN_DEPTH, budget)
        left = len(os.listdir(folder))
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
    last = state.get("check_at", 0)
    if now - last < CHECK_FLOOR:
        return "floor", f"checked {int(now - last)} s ago; at most once an hour", None, None
    state["check_at"] = now
    outcome, detail, release = _judged(survey(root))
    return outcome, detail, release, None


def _migrations(root: Path, env: Dict[str, str]) -> str:
    """The migration ledger's names, one line: what a start applied shows
    as a difference."""
    return _run(["docker", "exec", POSTGRES, "psql", "-U", env.get("POSTGRES_USER", "musicai"),
                 "-d", env.get("POSTGRES_DB", "music_ai"), "-Atc",
                 "SELECT string_agg(filename, ',' ORDER BY filename) FROM _schema_migrations"],
                cwd=root).stdout.decode().strip()


def _dump(root: Path, env: Dict[str, str]) -> Path:
    """The database before the move, streamed out of its own container into
    ./host/pre-update/ — one kept. The dump loaders' tables (DUMP_TABLES) are
    left out whole, schema too: a restore leaves them as they are."""
    folder = host_dir(root) / "pre-update"
    folder.mkdir(mode=0o700, exist_ok=True)
    tmp = folder / f".dump.{os.getpid()}"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        with os.fdopen(fd, "wb") as out:
            _run(["docker", "exec", POSTGRES, "pg_dump", "-U", env.get("POSTGRES_USER", "musicai"),
                  "-d", env.get("POSTGRES_DB", "music_ai"), "-Fc",
                  *[f"--exclude-table=public.{name}" for name in DUMP_TABLES]],
                 cwd=root, stdout=out, timeout=4 * 3600)
            out.flush()
            os.fsync(out.fileno())
        dump = folder / "pre-update.dump"
        os.replace(tmp, dump)
        return dump
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def _restore(root: Path, env: Dict[str, str], dump: Path) -> None:
    with open(dump, "rb") as source:
        _run(["docker", "exec", "-i", POSTGRES, "pg_restore", "-U", env.get("POSTGRES_USER", "musicai"),
              "-d", env.get("POSTGRES_DB", "music_ai"), "--clean", "--if-exists", "--single-transaction"],
             cwd=root, stdin=source, timeout=4 * 3600)


def _healthy(root: Path) -> bool:
    """The new node up and answering its health check, by Docker's own wait."""
    result = compose("up", "-d", "--no-build", "--wait", "--wait-timeout", "1800", root=root,
                     timeout=2000, check=False)
    if result.returncode != 0:
        said = (result.stderr or b"").decode("utf-8", "replace").strip().splitlines()
        log(f"the node did not come up healthy: {' / '.join(said[-3:]) or 'exit ' + str(result.returncode)}")
    return result.returncode == 0


def _front_restarted(root: Path, old: str, new: str) -> Optional[str]:
    """On a master — the machine whose owner installed the trusted front's
    user unit (scripts/master-front/) — the front comes back with an update
    that changed it. The agent never reads the node's identity: it is in a
    folder the node writes."""
    if _run(["systemctl", "--user", "cat", "sautium-front.service"], cwd=root, check=False).returncode != 0:
        return None
    if not git("diff", "--name-only", old, new, "--", "scripts/master-front/", root=root):
        return None
    _run(["systemctl", "--user", "restart", "sautium-front.service"], cwd=root)
    return "the trusted front restarted"


def verb_update(state: dict, now: float, root: Path = None) -> Tuple[str, str, Optional[str], Optional[str]]:
    """C4's update, unattended once the person asked: verify, build from a
    tree git made of the verified commit, stop, dump, move, start; the way
    back when the new node does not come up."""
    root = root or ROOT
    noop = state.get("update_noop_at", 0)
    if now - noop < UPDATE_NOOP_FLOOR:
        return "floor", "an update found nothing to do less than a day ago", None, None
    found = survey(root)
    state["check_at"] = now
    if not found["offered"]:
        outcome, detail, release = _judged(found)
        state["update_noop_at"] = now
        return ("current" if outcome == "current" else "refused"), detail, release, None
    name, commit = found["offered"]["name"], found["offered"]["commit"]
    if git("status", "--porcelain", "--untracked-files=no", root=root):
        state["update_noop_at"] = now
        return "refused", "the checkout has local changes: an update never moves a tree someone edits", name, None
    old_commit, old_image = head(root), image_of(root)
    env = read_env(root / ".env")
    tag = f"sautium-backend:{name}-{commit[:12]}"
    log(f"building {tag} from {name} ({commit})")
    archive = subprocess.Popen(["git", "archive", "--format=tar", f"{commit}:backend"], cwd=str(root),
                               stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    try:
        _run(["docker", "build", "-f", DOCKERFILE, "-t", tag, "-"], cwd=root,
             stdin=archive.stdout, timeout=4 * 3600)
    finally:
        archive.stdout.close()
        archive.wait()
    if archive.returncode != 0:
        return "failed", f"git archive of {name} failed: nothing moved", name, None
    log("stopping the node, then the dump")
    compose("stop", "backend", root=root)
    try:
        ledger = _migrations(root, env)
        dump = _dump(root, env)
    except Refusal:
        # The node as it was, and answering before the answer is sent
        _healthy(root)
        raise
    log(f"moving the checkout to {name} and starting it")
    try:
        git("-c", "advice.detachedHead=false", "checkout", "--quiet", "--detach", commit, root=root)
        set_env(root / ".env", "SAUTIUM_BACKEND_IMAGE", tag)
        healthy, why = _healthy(root), "did not come up healthy"
    except (Refusal, OSError) as e:
        healthy, why = False, f"could not be put in place ({e})"
    if healthy:
        former = (state.get("installed") or {}).get("previous")
        state["installed"] = {"name": name, "commit": commit, "at": now, "image": tag, "previous": old_image}
        state.pop("update_noop_at", None)
        state.pop("failed_since_installed", None)
        if former and former not in (tag, old_image) and former.startswith("sautium-backend:v"):
            _run(["docker", "image", "rm", former], cwd=root, check=False)
        front = _front_restarted(root, old_commit, commit)
        return "updated", f"moved to Sautium {name[1:]}" + (f"; {front}" if front else ""), name, None
    log(f"{name} {why}: the way back to {old_commit[:12]}")
    compose("stop", "backend", root=root, check=False)
    git("-c", "advice.detachedHead=false", "checkout", "--quiet", "--force", "--detach", old_commit, root=root)
    set_env(root / ".env", "SAUTIUM_BACKEND_IMAGE", old_image)
    restored, unrestored = _migrations(root, env) != ledger, None
    if restored:
        try:
            _restore(root, env, dump)
        except Refusal as e:
            # The release before starts all the same: its schema is older, its
            # migrations idempotent, and a node left down answers nobody
            unrestored = e.detail
            log(f"the database could not be put back ({unrestored}); the dump stays in {dump}")
    if not _healthy(root):
        return "failed", (f"Sautium {name[1:]} {why}, and the release before it did not start again "
                          "after the way back: see the node's log"), name, None
    if unrestored:
        return "failed", (f"Sautium {name[1:]} {why}: the node is back on the release before it, but "
                          f"its database could not be put back as it was ({unrestored})"), name, None
    return "undone", (f"Sautium {name[1:]} {why}: the node is back on the release before it"
                      + (", its database as it was before the update" if restored else "")), name, None


def _scrub(text: str) -> str:
    return SETUP_CODE.sub("XXXX-XXXX-XXXX", text)


def verb_diagnostics(state: dict, now: float, root: Path = None) -> Tuple[str, str, Optional[str], Optional[str]]:
    """F2's table: what the two containers logged, the power, the disk, the
    versions — no journal, no kernel log, no file listing."""
    root = root or ROOT
    last = state.get("diagnostics_at", 0)
    if now - last < DIAGNOSTICS_FLOOR:
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
    for name in (BACKEND, POSTGRES):
        section(f"log: {name}", ["docker", "logs", "--tail", str(LOG_TAIL), name])
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
    try:
        with opener.open(request, timeout=10) as response:
            return 200 <= response.status < 300
    except (urllib.error.URLError, OSError) as e:
        log(f"the node did not take the answer to {verb}: {e}")
        return False


# -- the starter ---------------------------------------------------------------

def starter_text(api: int = STARTER_API) -> bytes:
    return STARTER.format(api=api).encode()


def _starter_paths(root: Path = None) -> Tuple[Path, Path, Path]:
    bin_dir = host_dir(root) / "bin"
    return bin_dir / STARTER_NAME, bin_dir / f"{STARTER_NAME}.previous", bin_dir / "starter-trial"


def maintain_starter(state: dict, now: float, root: Path = None, start_probe=None) -> Optional[str]:
    """The starter's own update, under PI5-PLAN's four conditions; what
    happened, or None. A trial in progress ends here: the starter that ran
    this run is the new one, so it is kept."""
    current, previous, trial = _starter_paths(root)
    if trial.exists():
        trial.unlink()
        state["starter_since"] = now
        return "the new starter ran: kept"
    installed = state.get("installed")
    if not installed or now - installed["at"] < STARTER_AFTER or state.get("failed_since_installed"):
        return None
    wanted = starter_text()
    try:
        held = _read_bounded(current, STATE_MAX)
    except OSError:
        held = None
    if held == wanted or held is None or installed["commit"] != head(root):
        return None             # nothing to change, or HEAD is no release the agent verified and moved to
    _write_private(previous, held, 0o755)
    _write_private(trial, b"0\n", 0o600)
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
    """One start of the service: the requests drained, each asked verb run
    and answered, the starter looked after. 0 unless the body itself broke —
    a verb's failure is an answer, not a failed start."""
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
        for held in state.pop("unsent", []):
            if not answer(*held, key, api):
                state.setdefault("unsent", []).append(held)
        asked, _left = drain(root)
        for verb in asked:
            moment = time.time() if now is None else now
            try:
                outcome, detail, release, report = (verbs or VERB)[verb](state, moment, root)
            except Refusal as r:
                outcome, detail, release, report = r.outcome, r.detail, r.release, None
            except (OSError, subprocess.TimeoutExpired, ValueError) as e:
                outcome, detail, release, report = "failed", f"{type(e).__name__}: {e}", None, None
            if outcome == "failed":
                state["failed_since_installed"] = True
            log(f"{verb}: {outcome} — {detail}")
            if not answer(verb, outcome, detail, release, report, key, api):
                # The node was down (an update that could not start it); the
                # next run, whatever its request, says it first
                state.setdefault("unsent", []).append([verb, outcome, detail, release, None])
            save_state(state, root)
        note = maintain_starter(state, time.time() if now is None else now, root, start_probe)
        if note:
            log(note)
        save_state(state, root)
    finally:
        os.close(lock)
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
    service = (f"[Unit]\nDescription=Sautium host agent: %s\nOnFailure={UNIT}-restore.service\n\n"
               f"[Service]\nType=oneshot\nExecStart={starter}\nTimeoutStartSec=6h\n"
               f"StandardOutput=append:{log_file}\nStandardError=append:{log_file}\n"
               "NoNewPrivileges=yes\n")
    return {
        f"{UNIT}.path": (f"[Unit]\nDescription=Sautium host agent: the node's requests\n\n"
                         f"[Path]\nDirectoryNotEmpty={root}/run/requests\nUnit={UNIT}.service\n\n"
                         "[Install]\nWantedBy=default.target\n"),
        f"{UNIT}.service": service % "a request from the node",
        f"{UNIT}-probe.service": service % "a new starter tried",
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
    ctl("enable", "--now", f"{UNIT}.path")
    print(f"The host agent is installed for {root}.")
    user = os.environ.get("USER") or str(os.getuid())
    linger = _run(["loginctl", "show-user", user, "-p", "Linger", "--value"], cwd=root, check=False)
    if linger.stdout.decode().strip() != "yes":
        print("It runs while you are logged in. To keep it running with nobody logged in, the "
              f"machine's administrator runs once:\n    sudo loginctl enable-linger {user}")
    print("The node learns of the agent when it is made again: docker compose up -d")
    return 0


def uninstall(systemctl=None) -> int:
    ctl = systemctl or (lambda *args: _run(["systemctl", "--user", *args], check=False))
    ctl("disable", "--now", f"{UNIT}.path")
    folder = unit_dir()
    for name in units(ROOT):
        (folder / name).unlink(missing_ok=True)
    ctl("daemon-reload")
    print("The host agent's units are removed; ./host/ is kept (its key, the last dump).")
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
    parser.add_argument("command", choices=("run", "install", "uninstall", "status"))
    args = parser.parse_args(argv)
    if args.command == "run":
        return run()
    if args.command == "install":
        return install()
    if args.command == "uninstall":
        return uninstall()
    return status()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
