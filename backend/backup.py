"""Node backup on the backend — the job behind Settings > Library > Backup and
the `python -m backup` CLI (create / restore / inspect / selftest).

The format and the database drivers live in `desktop/node_backup.py`, shared
with the launcher's restore flow; this module binds them to THIS process:
its settings (database, identity dir, backup dir, PG_BIN), its password
check (`device_auth.verify_password` — the account is the KDF, nothing is
stored), its Argon2id semaphore, its load meter and its Library SSE channel.

The job. One at a time. The password is verified and turned into the KEK on
the request path (under the same semaphore as login: 256 MiB per derivation,
never unbounded), so the worker thread only ever holds a key. Progress is
bytes encrypted; the writer loop is the event source — every progress tick
wakes the library SSE subscribers (rate-limited to one wake a second, the
Library screen re-fetches on each), and `backup.done` / `backup.failed` are
the terminal states of the same job record.

Playback wins. The job runs pg_dump at below-normal priority and pauses
while the load meter reports playback (`mining_hold`, the identity miner's
rule): the subscribe callback flips a resume Event, the writer loop waits on
it between reads — an event from the meter, not a poll. pg_dump keeps its
snapshot open across the pause, so the file that results is still one
consistent picture of the database.
"""

import argparse
import getpass
import logging
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from config import settings
from desktop import node_backup as nb

logger = logging.getLogger(__name__)

MIN_FREE_BYTES = 3 * 1024 ** 3
NOTIFY_INTERVAL = 1.0

_state: Dict[str, Any] = {
    "running": False, "phase": "", "progress": "", "pct": None, "bytes": 0,
    "paused": None, "cancel_requested": False, "error": None, "done": None,
    "started_at": None,
}
_lock = threading.Lock()
_cancel = threading.Event()
_resume = threading.Event()


class JobError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


# ---------------------------------------------------------------------------
# This node
# ---------------------------------------------------------------------------

def backup_dir() -> Path:
    return Path(settings.backup_dir)


def pg_target(dbname: Optional[str] = None) -> nb.PgTarget:
    """The node's database through the backend's own credentials. On Docker
    that role is the superuser; under the launcher it is `sautium`, which is
    all a dump needs — the launcher's restore brings its own admin role."""
    return nb.PgTarget(
        host=settings.postgres_host, port=settings.postgres_port,
        dbname=dbname or settings.postgres_db,
        user=settings.postgres_user, password=settings.postgres_password,
        pg_bin=Path(settings.pg_bin) if settings.pg_bin else None)


def identity_dir() -> Optional[Path]:
    if not settings.p2p_identity_dir:
        return None
    d = Path(settings.p2p_identity_dir)
    return d if d.is_dir() else None


def node() -> Optional[dict]:
    """{"username", "pubkey"} of this node's account, or None."""
    from p2p_identity import resolve_identity
    ident = resolve_identity(settings)
    if not ident or not ident.get("username"):
        return None
    return {"username": ident["username"], "pubkey": ident["public_key_hex"].lower()}


def app_version() -> dict:
    """What wrote the file: the checkout's commit or the packaged build id."""
    try:
        from desktop.updater import current_commit, installed_build
        return {"commit": current_commit(), "build": installed_build()}
    except Exception as e:                                  # a packaged tree without git
        logger.debug("app version unavailable: %s", e)
        return {"commit": None, "build": None}


def availability() -> Dict[str, Any]:
    """Whether this node can make a backup at all, and why not."""
    import device_auth
    if node() is None:
        return {"available": False, "reason": "no_account"}
    if device_auth.account_anonymous():
        # The key is the account password; a minted password nobody has
        # ever seen can neither be typed here nor at a restore.
        return {"available": False, "reason": "anonymous"}
    try:
        pg_target().tool("pg_dump")
    except nb.BackupError:
        return {"available": False, "reason": "no_pg_dump"}
    return {"available": True, "reason": None}


def list_backups() -> List[dict]:
    """Every .sbk in the backup dir, newest first, with its header facts."""
    d = backup_dir()
    if not d.is_dir():
        return []
    me = node()
    out = []
    for path in sorted(d.glob("*" + nb.FILE_SUFFIX), key=lambda p: p.stat().st_mtime, reverse=True):
        entry: Dict[str, Any] = {"name": path.name, "size": path.stat().st_size}
        try:
            with open(path, "rb") as fp:
                header, _ = nb.read_header(fp)
            entry.update(created_at=header.get("created_at"),
                         username=header["node"]["username"], pubkey=header["node"]["pubkey"],
                         same_node=bool(me and me["pubkey"] == header["node"]["pubkey"]))
        except nb.BackupError as e:
            entry["error"] = str(e)
        out.append(entry)
    return out


def status() -> Dict[str, Any]:
    d = backup_dir()
    try:
        free = shutil.disk_usage(d if d.exists() else d.parent).free
    except OSError:
        free = None
    with _lock:
        job = dict(_state)
    from claude_code import is_launcher_mode
    return {"dir": str(d), "free_bytes": free, **availability(),
            "can_reveal": is_launcher_mode(), "files": list_backups(), "job": job}


# ---------------------------------------------------------------------------
# The job
# ---------------------------------------------------------------------------

def _notify() -> None:
    from routers.settings import notify_library_subscribers
    notify_library_subscribers()


def _fmt_bytes(n: int) -> str:
    if n >= 1024 ** 3:
        return f"{n / 1024 ** 3:.1f} GB"
    return f"{n / 1024 ** 2:.0f} MB"


async def start_from_request(password: str) -> None:
    """Settings > Library > Back up now. Raises JobError with the HTTP status
    the router should answer: 409 busy / not available, 401 password,
    507 disk."""
    import device_auth
    with _lock:
        if _state["running"]:
            raise JobError(409, "A backup is already running")
    avail = availability()
    if not avail["available"]:
        raise JobError(409, {"no_account": "This node has no account yet",
                             "anonymous": "Set a password for this identity first — the "
                                          "backup is encrypted with it",
                             "no_pg_dump": "pg_dump was not found (PG_BIN)"}[avail["reason"]])
    if not await device_auth.verify_password(password):
        raise JobError(401, "Wrong password")
    ident = node()
    d = backup_dir()
    d.mkdir(parents=True, exist_ok=True)
    files = list_backups()
    last = files[0]["size"] if files else 0
    need = max(2 * last, MIN_FREE_BYTES) if last else MIN_FREE_BYTES
    free = shutil.disk_usage(d).free
    if free < need:
        raise JobError(507, f"Not enough space in {d}: {_fmt_bytes(free)} free, "
                            f"{_fmt_bytes(need)} needed")
    kek = await device_auth.derive_guarded(nb.derive_kek, password, ident["username"])
    start(kek, ident["username"], ident["pubkey"], estimate=last)


def start(kek: bytes, username: str, pubkey: str, *, estimate: int = 0) -> None:
    with _lock:
        if _state["running"]:
            raise JobError(409, "A backup is already running")
        _state.update(running=True, phase="starting", progress="Starting…", pct=None,
                      bytes=0, paused=None, cancel_requested=False, error=None, done=None,
                      started_at=time.time())
    _cancel.clear()
    _resume.set()
    # A .part is a job that died with its process (one job per process, and
    # a live one removes its own on failure) — never a file worth keeping.
    for stale in backup_dir().glob("*" + nb.PART_SUFFIX):
        stale.unlink(missing_ok=True)
    threading.Thread(target=_worker, args=(kek, username, pubkey, estimate),
                     daemon=True, name="node-backup").start()


def cancel() -> None:
    with _lock:
        if not _state["running"]:
            raise JobError(409, "No backup running")
        _state["cancel_requested"] = True
    _cancel.set()
    _resume.set()          # a paused job must wake to notice
    _notify()


def _worker(kek: bytes, username: str, pubkey: str, estimate: int) -> None:
    from desktop.p2p import load_meter
    meter = load_meter.current()
    last_notify = [0.0]

    def publish(force: bool = False) -> None:
        now = time.monotonic()
        if force or now - last_notify[0] >= NOTIFY_INTERVAL:
            last_notify[0] = now
            _notify()

    def on_load(snap: dict) -> None:
        # Playback is the priority signal; the miner pauses on it and so do we.
        playing = bool(snap.get("playback"))
        with _lock:
            _state["paused"] = "playback" if playing else None
            if _state["running"]:
                _state["progress"] = ("Paused while playing — resumes when playback stops"
                                      if playing else _state["progress"])
        if playing:
            _resume.clear()
        else:
            _resume.set()
        publish(force=True)

    def wait_ok() -> None:
        _resume.wait()

    def progress(phase: str, **f) -> None:
        with _lock:
            _state["phase"] = phase
            if phase == "counting":
                _state["progress"] = "Taking a snapshot and counting rows…"
            elif phase == "dumping":
                b = int(f.get("bytes") or 0)
                _state["bytes"] = b
                _state["pct"] = min(99, int(b * 100 / estimate)) if estimate else None
                if _state["paused"] is None:
                    _state["progress"] = f"Backing up… {_fmt_bytes(b)}"
            elif phase == "identity":
                _state["progress"] = "Adding identity documents…"
        publish()

    if meter is not None:
        if meter.playback_active:
            _resume.clear()
            with _lock:
                _state["paused"] = "playback"
        meter.subscribe(on_load)
    version = app_version()
    try:
        result = nb.create_backup(
            backup_dir(), target=pg_target(), kek=kek, username=username, pubkey=pubkey,
            identity_dir=identity_dir(), app_commit=version["commit"], app_build=version["build"],
            progress=progress, cancel=_cancel, wait_ok=wait_ok)
        with _lock:
            _state.update(phase="done", pct=100, bytes=result["dump_size"],
                          progress=f"Done — {_fmt_bytes(result['size'])}",
                          done={"name": result["path"].name, "size": result["size"],
                                "sha256": result["sha256"],
                                "created_at": result["manifest"]["created_at"]})
        logger.info("backup done: %s (%d bytes)", result["path"], result["size"])
    except nb.Cancelled:
        with _lock:
            _state.update(phase="cancelled", progress="Cancelled", pct=None)
        logger.info("backup cancelled")
    except Exception as e:
        with _lock:
            _state.update(phase="failed", error=str(e), progress=f"Failed: {e}")
        logger.error("backup failed: %s", e, exc_info=True)
    finally:
        if meter is not None:
            meter.unsubscribe(on_load)
        with _lock:
            _state["running"] = False
            _state["paused"] = None
        publish(force=True)


def reveal_dir() -> None:
    """Open the backup folder in the host's file manager — launcher mode only
    (a container has no desktop to open it on)."""
    from claude_code import is_launcher_mode
    if not is_launcher_mode():
        raise JobError(409, "The folder is on the host: " + str(backup_dir()))
    d = backup_dir()
    d.mkdir(parents=True, exist_ok=True)
    if sys.platform == "win32":
        os.startfile(str(d))                       # type: ignore[attr-defined]
    elif sys.platform == "darwin":
        subprocess.Popen(["open", str(d)])
    else:
        subprocess.Popen(["xdg-open", str(d)])


# ---------------------------------------------------------------------------
# CLI — `python -m backup ...` inside the container (or any backend env)
# ---------------------------------------------------------------------------

def _password(args, *, prompt: str) -> str:
    var = args.password_env or ("SAUTIUM_BACKUP_PASSWORD" if os.environ.get("SAUTIUM_BACKUP_PASSWORD") else None)
    if var:
        value = os.environ.get(var)
        if not value:
            sys.exit(f"environment variable {var} is empty")
        return value
    return getpass.getpass(prompt)


def _print_progress(phase: str, **f) -> None:
    if phase in ("dumping", "restoring"):
        b = f.get("bytes") or 0
        total = f.get("total")
        line = f"\r{phase}… {b / 1e6:,.0f} MB" + (f" / {total / 1e6:,.0f} MB" if total else "")
        sys.stdout.write(line.ljust(60))
    else:
        sys.stdout.write(f"\n{phase}…")
    sys.stdout.flush()


def _cmd_create(args) -> int:
    ident = node()
    if ident is None:
        sys.exit("this node has no account — nothing to key the backup on")
    password = _password(args, prompt=f"Account password for {ident['username']}: ")
    from p2p_identity import derive_identity
    if derive_identity(ident["username"], password)["public_key_hex"].lower() != ident["pubkey"]:
        sys.exit("wrong password")
    out_dir = Path(args.out) if args.out else backup_dir()
    version = app_version()
    result = nb.create_backup(out_dir, target=pg_target(), kek=nb.derive_kek(password, ident["username"]),
                              username=ident["username"], pubkey=ident["pubkey"],
                              identity_dir=identity_dir(), app_commit=version["commit"],
                              app_build=version["build"], progress=_print_progress)
    print(f"\n{result['path']}  {result['size']:,} bytes  sha256 {result['sha256']}")
    return 0


def _cmd_inspect(args) -> int:
    import json
    password = None if args.no_password else _password(args, prompt="Backup password (Enter to skip): ")
    info = nb.inspect_file(Path(args.file), password or None)
    print(json.dumps(info, indent=2, default=str))
    return 0


def _other_sessions(target: nb.PgTarget) -> int:
    conn = target.connect(target.maintenance_db, admin=True)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM pg_stat_activity WHERE datname = %s "
                        "AND pid <> pg_backend_pid()", (target.dbname,))
            return int(cur.fetchone()[0])
    finally:
        conn.close()


def _cmd_restore(args) -> int:
    path = Path(args.file)
    target = pg_target(args.db)
    password = _password(args, prompt="Backup password: ")
    with open(path, "rb") as fp:
        reader = nb.BackupReader(fp)
        reader.unlock(password)
        manifest = reader.read_manifest()
        db = manifest["database"]
        print(f"backup of {manifest['node']['username']} ({manifest['node']['pubkey'][:16]}…) "
              f"from {manifest['created_at']}, PostgreSQL {db['server_version']}, "
              f"{len(db['tables'])} tables, {sum(db['tables'].values()):,} rows")
        nb.check_compatible(manifest)
        others = _other_sessions(target)
        if others and not args.yes:
            sys.exit(f"{others} other session(s) are connected to {target.dbname} — stop the "
                     "backend first, or pass --yes to terminate them")
        if not args.yes:
            answer = input(f"Restore into {target.dbname} on {target.host}:{target.port}? [y/N] ")
            if answer.strip().lower() != "y":
                return 1
        identity: Dict[str, bytes] = {}
        result = nb.restore_database(reader, target, manifest=manifest, replace=args.replace,
                                     progress=_print_progress, file_size=path.stat().st_size,
                                     identity_sink=identity.__setitem__)
    print(f"\nrestored {result['database']}; {result['migrations_applied']} newer migration(s) applied"
          + (f"; previous database kept as {result['previous']}" if result["previous"] else ""))
    if args.identity:
        d = Path(settings.p2p_identity_dir)
        out = nb.write_identity(d, identity, username=manifest["node"]["username"], password=password)
        print(f"identity written to {d}: {', '.join(out['files'])}"
              + (f" (previous identity moved to {out['moved_previous_identity']})"
                 if out["moved_previous_identity"] else ""))
    elif identity:
        print(f"identity documents NOT written ({len(identity)} in the backup) — pass --identity "
              "to make this machine the node")
    return 0


def _cmd_selftest(args) -> int:
    ident = node() or {"username": "selftest", "pubkey": "00" * 32}
    ok = nb.selftest(pg_target(), test_db=args.db, identity_dir=identity_dir(),
                     pubkey=ident["pubkey"], username=ident["username"],
                     out_dir=Path(args.out) if args.out else None, keep=args.keep)
    return 0 if ok else 1


def main(argv: Optional[List[str]] = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    ap = argparse.ArgumentParser(prog="backup", description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    pw = argparse.ArgumentParser(add_help=False)
    pw.add_argument("--password-env", metavar="VAR",
                    help="read the password from this environment variable "
                         "(default: SAUTIUM_BACKUP_PASSWORD when set, else prompt)")
    c = sub.add_parser("create", parents=[pw], help="write a backup of this node")
    c.add_argument("--out", help="directory (default: the node's backup dir)")
    i = sub.add_parser("inspect", parents=[pw], help="show what a backup holds")
    i.add_argument("file")
    i.add_argument("--no-password", action="store_true", help="header only")
    r = sub.add_parser("restore", parents=[pw],
                       help="rebuild a database from a backup (stop the backend first)")
    r.add_argument("file")
    r.add_argument("--db", help="target database (default: the node's)")
    r.add_argument("--replace", action="store_true",
                   help="replace a database that holds own data (kept as <db>__previous)")
    r.add_argument("--identity", action="store_true",
                   help="write the identity documents too — this machine becomes the node")
    r.add_argument("--yes", action="store_true", help="no questions; terminate other sessions")
    s = sub.add_parser("selftest", help="dump → restore into a test database → compare counts")
    s.add_argument("--db", default="music_ai_test")
    s.add_argument("--out", help="where the temporary backup file goes")
    s.add_argument("--keep", action="store_true", help="keep the test database and the file")
    args = ap.parse_args(argv)
    try:
        return {"create": _cmd_create, "inspect": _cmd_inspect,
                "restore": _cmd_restore, "selftest": _cmd_selftest}[args.cmd](args)
    except nb.BackupError as e:
        sys.exit(f"error: {e}")


if __name__ == "__main__":
    sys.exit(main())
