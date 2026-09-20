"""Mechanics shared by the dump loaders (``mb_dump_load`` for the MusicBrainz
subset, ``lb_dump_load`` for the ListenBrainz statistics): the resumable
mirror download, the progress-counting file wrapper, checksum verification
and the index drop/rebuild helpers. One copy — a Range/resume or watcher fix
lands in both loaders at once.
"""

import hashlib
import json
import logging
import os
import threading
import time
from typing import Callable, Dict, Optional

logger = logging.getLogger("dump_common")

# progress_cb(update: dict) — every loader speaks the same phase vocabulary
# (checking | downloading | verifying | loading | aggregating | indexing |
# analyzing | genres | done); the job runner turns it into a status line.
ProgressCb = Callable[[Dict], None]


def noop(_: Dict) -> None: ...


class ProgressReader:
    """Wraps a file object, reporting bytes-read (throttled) as a consumer
    drains it — so a multi-GB COPY or decompression shows intra-step
    progress instead of a 60s stall."""

    def __init__(self, fh, on_bytes: Callable[[int], None]):
        self._fh = fh
        self._on = on_bytes
        self._read = 0
        self._last = 0.0

    def _count(self, n: int) -> None:
        if n:
            self._read += n
            now = time.monotonic()
            if now - self._last > 0.3:
                self._last = now
                self._on(self._read)

    def read(self, size: int = -1) -> bytes:
        chunk = self._fh.read(size)
        self._count(len(chunk))
        return chunk

    def readline(self, size: int = -1) -> bytes:
        line = self._fh.readline(size)
        self._count(len(line))
        return line

    def __getattr__(self, name):  # forward close/etc. to the real file
        return getattr(self._fh, name)


# ── download (resumable) ─────────────────────────────────────────────────────

def download_resumable(url: str, path: str, progress_cb: ProgressCb = noop, *,
                       label: str, attempts: int = 5,
                       headers: Optional[Dict[str, str]] = None) -> None:
    """Resumable streaming download of ``url`` into ``path``. Retries on
    transport/timeout errors, RESUMING from the bytes already on disk (Range)
    — the MetaBrainz mirror intermittently drops the TLS handshake or stalls
    mid-stream, and a multi-GB transfer must survive a blip instead of
    restarting. A 416 means the file on disk is already complete."""
    import httpx
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    t0 = time.monotonic()
    for attempt in range(attempts):
        have = os.path.getsize(path) if os.path.exists(path) else 0
        hdrs = dict(headers or {})
        if have:
            hdrs["Range"] = f"bytes={have}-"
        try:
            with httpx.stream("GET", url, headers=hdrs,
                              timeout=httpx.Timeout(120.0, connect=30.0, read=120.0),
                              follow_redirects=True) as r:
                if r.status_code == 416:  # already fully downloaded
                    return
                resuming = r.status_code == 206
                r.raise_for_status()
                total = (have if resuming else 0) + int(r.headers.get("Content-Length", 0))
                done = have if resuming else 0
                a_start, a_bytes = time.monotonic(), 0
                with open(path, "ab" if resuming else "wb") as fh:
                    last = 0.0
                    for chunk in r.iter_bytes(1 << 20):
                        fh.write(chunk)
                        done += len(chunk)
                        a_bytes += len(chunk)
                        now = time.monotonic()
                        # Reconnect on a stuck-slow connection (data trickles, so
                        # the read timeout never fires): a fresh TCP path is often
                        # fast (mirror throughput is variable). Resumes via Range.
                        el = now - a_start
                        if el > 45 and a_bytes / el < 600_000:  # < 0.6 MB/s sustained
                            raise httpx.ReadTimeout(f"{label} download too slow — reconnecting")
                        if now - last > 0.5:  # throttle progress emits
                            last = now
                            pct = round(done / total * 100, 1) if total else 0
                            progress_cb({"phase": "downloading", "pct": pct,
                                         "downloaded_mb": round(done / 1e6),
                                         "total_mb": round(total / 1e6)})
            logger.info("%s download done in %.0fs (%.2f GB)", label,
                        time.monotonic() - t0, os.path.getsize(path) / 1e9)
            return
        except (httpx.TransportError, httpx.TimeoutException) as e:
            logger.warning("%s download interrupted at %d bytes (try %d/%d): %s",
                           label, have, attempt + 1, attempts, e)
            if attempt == attempts - 1:
                raise
            time.sleep(3)


# ── checksums ────────────────────────────────────────────────────────────────

def sha256_file(path: str, on_bytes: Optional[Callable[[int], None]] = None) -> str:
    """Hex SHA-256 of a file, 4 MiB blocks; ``on_bytes`` (throttled) carries
    the bytes hashed so far so a 20 GB verification moves a progress bar."""
    h = hashlib.sha256()
    done, last = 0, 0.0
    with open(path, "rb") as f:
        for blk in iter(lambda: f.read(1 << 22), b""):
            h.update(blk)
            done += len(blk)
            now = time.monotonic()
            if on_bytes and now - last > 0.3:
                last = now
                on_bytes(done)
    return h.hexdigest()


def verify_sha256(path: str, expected_hex: str,
                  on_bytes: Optional[Callable[[int], None]] = None) -> None:
    """Fail closed: a mismatch raises, the caller decides about the file."""
    got = sha256_file(path, on_bytes)
    if got != expected_hex.strip().lower():
        raise RuntimeError(f"checksum mismatch for {os.path.basename(path)}: "
                           f"expected {expected_hex}, got {got}")


# ── index drop/rebuild around a bulk load ────────────────────────────────────
# COPY into indexed tables maintains every index row-by-row; dropping indexes
# + PK/UNIQUE constraints first and rebuilding after replaces incremental
# maintenance with sorted bottom-up builds (parallel on PG18, GIN included).
# A DDL snapshot persisted next to the archive makes a crash at any point
# recoverable: the next run merges it with the live catalogs and rebuilds.

def collect_index_ddl(conn, tables, saved_ddl_path: str) -> Dict[str, Dict[str, str]]:
    """{table: {name: DDL}} for every index and PK/UNIQUE constraint, from the
    live catalogs (the source of truth — 001 drift included). Merged with the
    crash-file from an interrupted run, where the live set is already partial;
    live definitions win on name collisions. The merged snapshot is persisted
    BEFORE anything is dropped, so a crash anywhere in the load can always
    rebuild the full original set on the next run."""
    saved: Dict[str, Dict[str, str]] = {}
    if os.path.exists(saved_ddl_path):
        with open(saved_ddl_path) as f:
            saved = json.load(f)
        logger.warning("resuming with saved index DDL from %s", saved_ddl_path)
    ddl: Dict[str, Dict[str, str]] = {}
    with conn.cursor() as cur:
        for table in tables:
            merged = dict(saved.get(table, {}))
            cur.execute(
                "SELECT conname, pg_get_constraintdef(oid) FROM pg_constraint "
                "WHERE conrelid = %s::regclass AND contype IN ('p', 'u')", (table,))
            for name, condef in cur.fetchall():
                merged[name] = f"ALTER TABLE {table} ADD CONSTRAINT {name} {condef}"
            cur.execute(
                "SELECT indexname, indexdef FROM pg_indexes "
                "WHERE schemaname = 'public' AND tablename = %s", (table,))
            for name, idxdef in cur.fetchall():
                # constraint-owned indexes (pkey) keep the ALTER form from above
                merged.setdefault(name, idxdef)
            ddl[table] = merged
    with open(saved_ddl_path, "w") as f:
        json.dump(ddl, f, indent=1)
    return ddl


def drop_indexes(cur, table: str, ddl: Dict[str, str]) -> None:
    for name, stmt in ddl.items():
        if stmt.startswith("ALTER TABLE"):
            cur.execute(f"ALTER TABLE {table} DROP CONSTRAINT IF EXISTS {name}")
        else:
            cur.execute(f"DROP INDEX IF EXISTS {name}")


def watch_index_build(pid: int, i: int, n: int,
                      on_frac: Callable[[float], None],
                      stop: threading.Event) -> None:
    """Report intra-index progress from ``pg_stat_progress_create_index``
    while the loader connection is blocked inside CREATE INDEX. A 1s poll on
    a second pooled connection is the only source PG offers for utility-
    command progress (no push channel exists). Watcher failure must never
    fail the load — the bar just falls back to one step per index."""
    from db_pool import get_conn
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                while not stop.wait(1.0):
                    cur.execute(
                        "SELECT blocks_done, blocks_total, tuples_done, tuples_total "
                        "FROM pg_stat_progress_create_index WHERE pid = %s", (pid,))
                    row = cur.fetchone()
                    if not row:
                        continue
                    bd, bt, td, tt = row
                    intra = (bd / bt) if bt else ((td / tt) if tt else 0.0)
                    on_frac((i + min(intra, 1.0)) / n)
    except Exception as e:
        logger.warning("index-build progress watcher stopped: %s", e)


def rebuild_indexes(conn, ddl: Dict[str, str],
                    on_frac: Callable[[float], None]) -> None:
    """Each build in its own transaction: SET LOCAL scopes the memory/parallel
    bump to the statement, and a write-free transaction is what allows the
    parallel (leader+workers) build path at all. ``on_frac`` gets the
    rebuild fraction [0..1], fed between indexes AND (via the watcher)
    inside each multi-minute build. ``conn`` must be in autocommit mode."""
    n = len(ddl)
    if not n:
        return
    with conn.cursor() as cur:
        cur.execute("SELECT pg_backend_pid()")
        pid = cur.fetchone()[0]
    for i, stmt in enumerate(ddl.values()):
        on_frac(i / n)
        stop = threading.Event()
        watcher = threading.Thread(target=watch_index_build,
                                   args=(pid, i, n, on_frac, stop), daemon=True)
        watcher.start()
        try:
            with conn.cursor() as cur:
                cur.execute("BEGIN")
                cur.execute("SET LOCAL maintenance_work_mem = '1GB'")
                cur.execute("SET LOCAL max_parallel_maintenance_workers = 4")
                cur.execute(stmt)
                cur.execute("COMMIT")
        finally:
            stop.set()
            watcher.join()
    on_frac(1.0)


def unlock_all(conn) -> None:
    """Release a session-level advisory lock held on a POOLED connection. A
    failed statement leaves an explicit transaction aborted — clear it first
    or the unlock itself would fail and the lock would outlive the load."""
    from psycopg2.extensions import TRANSACTION_STATUS_IDLE
    if conn.info.transaction_status != TRANSACTION_STATUS_IDLE:
        with conn.cursor() as cur:
            cur.execute("ROLLBACK")
    with conn.cursor() as cur:
        cur.execute("SELECT pg_advisory_unlock_all()")


def table_stats(tables) -> Dict[str, Dict[str, int]]:
    """{relname: {est_rows, bytes}} from reltuples + pg_total_relation_size —
    no count(*) scans; exact right after the loaders' ANALYZE."""
    from db_pool import db_query
    rows = db_query("""
        SELECT c.relname,
               GREATEST(c.reltuples, 0)::bigint AS est_rows,
               pg_total_relation_size(c.oid) AS bytes
        FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = 'public' AND c.relname = ANY(%(t)s)
    """, {"t": list(tables)})
    return {r["relname"]: {"est_rows": int(r["est_rows"]), "bytes": int(r["bytes"])}
            for r in rows}
