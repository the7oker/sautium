"""Node backup on the backend — `python -m backup create|inspect|restore|selftest`.

The format and the database drivers are `desktop/node_backup.py`; this module
binds them to a node — its database, identity dir, backup dir and PostgreSQL
tools — and is the ONE implementation of "make a backup" for every caller:

  * the Docker node's weekly task: `docker exec sautium-backend python -m
    backup create --password-env P2P_PASSWORD` (sautium-private/scripts/
    backup.sh). Entry point, subcommand, `--password-env`, the default
    output dir (BACKUP_DIR, `/app/data/backup`) and the `.sbk` suffix are a
    contract that script relies on; a non-zero exit is its failure signal;
  * the launcher's Settings & Tools › Backup & Restore › "Create backup…", which runs
    this same CLI on the backend interpreter (desktop/backup_task.py) with
    `--progress-json --cancel-on-stdin`;
  * a hand-run CLI on either interpreter.

Two runtimes, one module. Under the launcher the backend's configuration is
`<data_dir>/backend.env` — service_manager hands it to the backend process,
but a hand-run `python -m backup` has no such parent — so the module loads
that file into its environment BEFORE `config` builds its Settings: the same
DSN, identity dir, BACKUP_DIR and PG_BIN (pgsql/bin) the backend runs with.
In a container there is no such file and compose has set the environment.

The policy is the same everywhere. The account password is verified by
re-deriving the identity (device_auth.verify_password — the login path, under
its Argon2id semaphore), pg_dump runs at below-normal priority, and the job
pauses while the node plays music. Playback is known to the backend process
and the job runs in another, so the signal crosses through PostgreSQL: the
backend holds a session advisory lock (PLAYBACK_LOCK_KEY) for as long as its
load meter reports playback (PlaybackSignal, driven by the meter's samples),
and the job waits on that lock between reads (PlaybackHold) —
`pg_advisory_lock` blocks in the server until the holder releases, so the
wait is an event, not a poll, and a backend that dies takes its lock with
it, so the job can never hang on a stale flag.

There is deliberately no Web UI surface (removed 2026-09-14): a browser
cannot receive a 3 GB file, and the password that keys it is typed where the
file lands — the launcher, or the shell that owns the node.
"""

import argparse
import asyncio
import getpass
import json
import logging
import os
import shutil
import sys
import threading
from pathlib import Path
from typing import Callable, Dict, List, Optional

import psycopg2
import psycopg2.errors


def _bootstrap_launcher_env() -> None:
    """A hand-run CLI under the launcher: the backend's generated environment
    (DSN, identity dir, BACKUP_DIR, PG_BIN), applied before `config` reads
    it. Values already in the environment win (the backend process itself,
    an explicit override); a container has no such file."""
    if Path("/.dockerenv").exists() or Path("/run/.containerenv").exists():
        return
    try:
        from desktop.config_manager import get_data_dir, load_env_file
    except ImportError:
        return
    path = get_data_dir() / "backend.env"
    if path.exists():
        for key, value in load_env_file(path).items():
            os.environ.setdefault(key, value)


_bootstrap_launcher_env()

from config import settings  # noqa: E402
from desktop import node_backup as nb  # noqa: E402

logger = logging.getLogger(__name__)

MIN_FREE_BYTES = 3 * 1024 ** 3
# One key, one meaning: "this node is playing". Held by the backend's session
# while its load meter reports playback; waited on by a running backup.
PLAYBACK_LOCK_KEY = 0x53415554        # "SAUT"


# ---------------------------------------------------------------------------
# This node
# ---------------------------------------------------------------------------

def backup_dir() -> Path:
    return Path(settings.backup_dir)


def pg_target(dbname: Optional[str] = None) -> nb.PgTarget:
    """The node's database. Docker's role is a superuser already; the
    launcher's cluster has `postgres` (superuser) and `sautium` (the app)
    under one password (desktop/db_init.create_database), and a restore
    needs the former to create and swap databases."""
    from claude_code import is_launcher_mode
    launcher = is_launcher_mode()
    return nb.PgTarget(
        host=settings.postgres_host, port=settings.postgres_port,
        dbname=dbname or settings.postgres_db,
        user=settings.postgres_user, password=settings.postgres_password,
        admin_user="postgres" if launcher else None,
        admin_password=settings.postgres_password if launcher else None,
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


def _fmt_bytes(n: int) -> str:
    if n >= 1024 ** 3:
        return f"{n / 1024 ** 3:.1f} GB"
    return f"{n / 1024 ** 2:.0f} MB"


# ---------------------------------------------------------------------------
# Playback signal — backend side (holder) and job side (waiter)
# ---------------------------------------------------------------------------

class PlaybackSignal:
    """Owned by the backend (main.py): mirrors the load meter's playback
    flag into a session advisory lock. Reconciled on every meter sample, so
    a `pg_try_advisory_lock` that lost to a waiter's momentary hold is retried
    two seconds later; nothing happens on a sample that changes nothing."""

    def __init__(self, dsn: str):
        self._dsn = dsn
        self._conn = None
        self._held = False
        self._lock = threading.Lock()

    def reconcile(self, playing: bool) -> None:
        with self._lock:
            if playing == self._held:
                return
            try:
                if self._conn is None or self._conn.closed:
                    self._conn = psycopg2.connect(self._dsn)
                    self._conn.autocommit = True
                    self._held = False
                    if not playing:
                        return
                with self._conn.cursor() as cur:
                    if playing:
                        cur.execute("SELECT pg_try_advisory_lock(%s)", (PLAYBACK_LOCK_KEY,))
                        self._held = bool(cur.fetchone()[0])
                    else:
                        cur.execute("SELECT pg_advisory_unlock(%s)", (PLAYBACK_LOCK_KEY,))
                        self._held = False
            except psycopg2.Error as e:
                logger.debug("playback signal: %s", e)
                self._drop()

    def _drop(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            except psycopg2.Error:
                pass
        self._conn = None
        self._held = False

    def close(self) -> None:
        with self._lock:
            self._drop()


class PlaybackHold:
    """The job's side: a dedicated session that returns at once while nobody
    plays and otherwise blocks on the lock until the backend releases it.
    `cancel()` from another thread aborts a blocked wait (the server cancels
    the statement) so a cancelled job does not outlive the music."""

    def __init__(self, dsn: str):
        self._conn = psycopg2.connect(dsn)
        self._conn.autocommit = True

    def wait(self, on_pause: Optional[Callable[[], None]] = None) -> None:
        with self._conn.cursor() as cur:
            cur.execute("SELECT pg_try_advisory_lock(%s)", (PLAYBACK_LOCK_KEY,))
            if cur.fetchone()[0]:
                cur.execute("SELECT pg_advisory_unlock(%s)", (PLAYBACK_LOCK_KEY,))
                return
            if on_pause is not None:
                on_pause()
            try:
                cur.execute("SELECT pg_advisory_lock(%s)", (PLAYBACK_LOCK_KEY,))
            except psycopg2.errors.QueryCanceled:
                raise nb.Cancelled("backup cancelled")
            cur.execute("SELECT pg_advisory_unlock(%s)", (PLAYBACK_LOCK_KEY,))

    def cancel(self) -> None:
        try:
            self._conn.cancel()
        except psycopg2.Error:
            pass

    def close(self) -> None:
        try:
            self._conn.close()
        except psycopg2.Error:
            pass


class CancelToken:
    """Request-from-anywhere cancellation: sets the flag the writer loop
    checks between reads and aborts a wait blocked on the playback lock."""

    def __init__(self):
        self.event = threading.Event()
        self._hold: Optional[PlaybackHold] = None

    def request(self) -> None:
        self.event.set()
        if self._hold is not None:
            self._hold.cancel()


# ---------------------------------------------------------------------------
# Create — the one entry every caller goes through
# ---------------------------------------------------------------------------

def create(password: str, *, out_dir: Optional[Path] = None,
           progress: Optional[nb.ProgressFn] = None,
           cancel: Optional[CancelToken] = None) -> dict:
    """Verify the password against this node's identity, then write the
    backup. Raises node_backup errors (WrongPassword, Refused, Cancelled,
    BackupError); returns node_backup.create_backup's result."""
    import device_auth

    progress = progress or (lambda *_a, **_k: None)
    cancel = cancel or CancelToken()
    ident = node()
    if ident is None:
        raise nb.Refused("this node has no account — nothing to key the backup on")
    if device_auth.account_anonymous():
        raise nb.Refused("this identity's password was minted and never shown — "
                         "set one (Profile) before backing up")
    if not asyncio.run(device_auth.verify_password(password)):
        raise nb.WrongPassword("wrong password")
    kek = nb.derive_kek(password, ident["username"])

    out = Path(out_dir) if out_dir else backup_dir()
    out.mkdir(parents=True, exist_ok=True)
    # A .part is a job that died with its process (a live one removes its own
    # on failure) — never a file worth keeping.
    for stale in out.glob("*" + nb.PART_SUFFIX):
        stale.unlink(missing_ok=True)
    have = sorted(out.glob("*" + nb.FILE_SUFFIX), key=lambda p: p.stat().st_mtime)
    last = have[-1].stat().st_size if have else 0
    need = max(2 * last, MIN_FREE_BYTES) if last else MIN_FREE_BYTES
    free = shutil.disk_usage(out).free
    if free < need:
        raise nb.Refused(f"not enough space in {out}: {_fmt_bytes(free)} free, "
                         f"{_fmt_bytes(need)} needed")

    hold = PlaybackHold(settings.database_url)
    cancel._hold = hold
    version = app_version()

    def wait_ok() -> None:
        if cancel.event.is_set():
            return                                   # create_backup raises Cancelled next
        hold.wait(on_pause=lambda: progress("paused", reason="playback"))

    try:
        return nb.create_backup(
            out, target=pg_target(), kek=kek, username=ident["username"],
            pubkey=ident["pubkey"], identity_dir=identity_dir(),
            app_commit=version["commit"], app_build=version["build"],
            progress=progress, cancel=cancel.event, wait_ok=wait_ok)
    finally:
        cancel._hold = None
        hold.close()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _password(args, *, prompt: str) -> str:
    var = args.password_env or ("SAUTIUM_BACKUP_PASSWORD"
                                if os.environ.get("SAUTIUM_BACKUP_PASSWORD") else None)
    if var:
        value = os.environ.get(var)
        if not value:
            sys.exit(f"environment variable {var} is empty")
        return value
    return getpass.getpass(prompt)


class HumanProgress:
    """The terminal form: a redrawn byte counter (the weekly task's log filter
    strips `counting…` / `dumping…` / `identity…` lines — keep those words)."""

    def __call__(self, phase: str, **f) -> None:
        if phase in ("dumping", "restoring"):
            b = f.get("bytes") or 0
            total = f.get("total")
            line = f"\r{phase}… {b / 1e6:,.0f} MB" + (f" / {total / 1e6:,.0f} MB" if total else "")
            sys.stdout.write(line.ljust(60))
        elif phase == "paused":
            sys.stdout.write(f"\npaused while playing…")
        else:
            sys.stdout.write(f"\n{phase}…")
        sys.stdout.flush()

    def done(self, result: dict) -> None:
        print(f"\n{result['path']}  {result['size']:,} bytes  sha256 {result['sha256']}")

    def failed(self, message: str) -> None:
        print(f"\nerror: {message}", file=sys.stderr)

    def cancelled(self) -> None:
        print("\ncancelled")


class JsonProgress:
    """One JSON object per line on stdout — what the launcher reads."""

    def _emit(self, obj: dict) -> None:
        sys.stdout.write(json.dumps(obj) + "\n")
        sys.stdout.flush()

    def __call__(self, phase: str, **f) -> None:
        self._emit({"phase": phase, **{k: v for k, v in f.items() if v is not None}})

    def done(self, result: dict) -> None:
        self._emit({"phase": "done", "path": str(result["path"]), "name": result["path"].name,
                    "size": result["size"], "sha256": result["sha256"],
                    "dump_size": result["dump_size"]})

    def failed(self, message: str) -> None:
        self._emit({"phase": "error", "message": message})

    def cancelled(self) -> None:
        self._emit({"phase": "cancelled"})


def watch_stdin_for_cancel(token: CancelToken, stream=None) -> threading.Thread:
    """`--cancel-on-stdin`: a line saying `cancel` — or the end of the stream,
    which is the parent going away — cancels the job. Off by default: the
    weekly task's stdin is /dev/null and must not count as a cancel."""
    def watch() -> None:
        for line in (stream or sys.stdin):
            if line.strip().lower() == "cancel":
                break
        token.request()
    t = threading.Thread(target=watch, daemon=True, name="backup-stdin")
    t.start()
    return t


def _cmd_create(args) -> int:
    password = _password(args, prompt="Account password: ")
    printer = JsonProgress() if args.progress_json else HumanProgress()
    token = CancelToken()
    if args.cancel_on_stdin:
        watch_stdin_for_cancel(token)
    try:
        result = create(password, out_dir=Path(args.out) if args.out else None,
                        progress=printer, cancel=token)
    except nb.Cancelled:
        printer.cancelled()
        return 1
    except nb.BackupError as e:
        printer.failed(str(e))
        return 1
    printer.done(result)
    return 0


def _cmd_inspect(args) -> int:
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
    printer = HumanProgress()
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
                                     progress=printer, file_size=path.stat().st_size,
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
    # A Windows console on a legacy code page cannot encode the arrows and
    # ellipses in the help and progress text; a hand-run CLI must not die on
    # its own output (the launcher's child runs with PYTHONUTF8=1 anyway).
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")
    ap = argparse.ArgumentParser(prog="backup", description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    pw = argparse.ArgumentParser(add_help=False)
    pw.add_argument("--password-env", metavar="VAR",
                    help="read the password from this environment variable "
                         "(default: SAUTIUM_BACKUP_PASSWORD when set, else prompt)")
    c = sub.add_parser("create", parents=[pw], help="write a backup of this node")
    c.add_argument("--out", help="directory (default: the node's backup dir)")
    c.add_argument("--progress-json", action="store_true",
                   help="one JSON object per line on stdout instead of the terminal counter")
    c.add_argument("--cancel-on-stdin", action="store_true",
                   help="a `cancel` line (or EOF) on stdin cancels the job")
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
