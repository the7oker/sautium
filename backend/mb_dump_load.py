"""Download + load the MusicBrainz fullexport subset into the local ``mb_*`` tables.

The engine behind the canonical-data roadmap: one ~7 GB download replaces tens
of thousands of throttled MB-API calls with unlimited local joins. Loads only
the artist + release-group subset — no ``recording`` (35 M rows, skipped).

ECONOMICAL load path (no intermediate extracted files at all): decompress the
tarball ONCE through ``lbzip2``/``bzip2`` and pipe each needed member's TSV
STRAIGHT into ``COPY mb_x FROM STDIN`` over a normal psycopg2 connection. Peak
disk is just the archive; peak memory is one COPY buffer. Works in-process in
both the Docker backend and the desktop launcher (both reach Postgres over the
network), and as a CLI. Idempotent: ``TRUNCATE`` + reload per table.

Column order in the headerless dump TSV == MB ``CreateTables.sql`` order; the
``mb_*`` definitions (001_initial.sql) mirror it, so a default ``COPY`` round-
trips it and a schema drift fails loudly on field count.
"""

import argparse
import hashlib
import logging
import os
import shutil
import subprocess
import tarfile
import time
from typing import Callable, Dict, Optional

logger = logging.getLogger("mb_dump_load")

_HERE = os.path.dirname(os.path.abspath(__file__))
_DATA = os.environ.get("MB_DUMP_DIR") or os.path.normpath(os.path.join(_HERE, "..", "data", "mbdump"))
DUMP_PATH = os.path.join(_DATA, "mbdump.tar.bz2")
VERSION_PATH = os.path.join(_DATA, "VERSION")

_MIRROR = "https://data.metabrainz.org/pub/musicbrainz/data/fullexport"

# mb_* table -> dump member basename (under mbdump/ in the tarball).
TABLES = [
    ("mb_area", "area"),
    ("mb_release_group_primary_type", "release_group_primary_type"),
    ("mb_release_group_secondary_type", "release_group_secondary_type"),
    ("mb_artist", "artist"),
    ("mb_artist_alias", "artist_alias"),
    ("mb_artist_credit", "artist_credit"),
    ("mb_artist_credit_name", "artist_credit_name"),
    ("mb_release_group", "release_group"),
    ("mb_release_group_secondary_type_join", "release_group_secondary_type_join"),
    ("mb_release", "release"),
]
_MEMBER_TABLE = {f"mbdump/{m}": t for t, m in TABLES}

# Approx fraction of total load each table represents (by TSV size) — so the bar
# is byte-weighted (smooth) instead of one even 1/10 step per table (which jumps
# unevenly: a 13 MB table and a 750 MB table are NOT each 10% of the work).
# Stable across dump versions (tables grow together); sums to ~1.0.
_LOAD_WEIGHT = {
    "mb_artist": 0.18, "mb_artist_credit": 0.17, "mb_release": 0.31,
    "mb_release_group": 0.20, "mb_artist_credit_name": 0.10,
    "mb_artist_alias": 0.02, "mb_area": 0.01,
    "mb_release_group_secondary_type_join": 0.01,
    "mb_release_group_primary_type": 0.0, "mb_release_group_secondary_type": 0.0,
}

# progress_cb(update: dict). Phases: checking|downloading|loading|analyzing|done|error.
ProgressCb = Callable[[Dict], None]
def _noop(_: Dict) -> None: ...


class _ProgressReader:
    """Wraps a file object, reporting bytes-read (throttled) as ``COPY`` drains
    it — so the big tables show intra-step progress instead of a 60s stall."""
    def __init__(self, fh, on_bytes: Callable[[int], None]):
        self._fh = fh
        self._on = on_bytes
        self._read = 0
        self._last = 0.0

    def read(self, size: int = -1) -> bytes:
        chunk = self._fh.read(size)
        if chunk:
            self._read += len(chunk)
            now = time.monotonic()
            if now - self._last > 0.3:
                self._last = now
                self._on(self._read)
        return chunk

    def __getattr__(self, name):  # forward readline/close/etc. to the real file
        return getattr(self._fh, name)


def _decompressor() -> Optional[list]:
    """Parallel lbzip2 (≈1 min on a many-core box) if available, else bzip2.
    None → no external tool; fall back to Python's (slower) bz2 stream."""
    for exe in ("lbzip2", "pbzip2", "bzip2"):
        path = shutil.which(exe) or shutil.which(
            os.path.join(os.path.expanduser("~"), "miniconda3", "bin", exe))
        if path:
            return [path, "-dc"]
    return None


# ── version ──────────────────────────────────────────────────────────────────

def latest_version() -> Optional[str]:
    """The newest fullexport version string (e.g. '20260530-001914'), or None
    if the mirror is unreachable. Retried — the mirror intermittently times out
    the TLS handshake, and one blip shouldn't fail the whole update."""
    import httpx
    for attempt in range(3):
        try:
            r = httpx.get(f"{_MIRROR}/LATEST",
                          timeout=httpx.Timeout(30.0, connect=30.0))
            r.raise_for_status()
            return r.text.strip() or None
        except Exception as e:
            logger.warning(f"MB latest-version check failed (try {attempt + 1}/3): {e}")
            if attempt < 2:
                time.sleep(2)
    return None


def loaded_version() -> Optional[str]:
    try:
        with open(VERSION_PATH) as f:
            return f.read().strip() or None
    except FileNotFoundError:
        return None


# ── download (resumable) ─────────────────────────────────────────────────────

def download(version: str, progress_cb: ProgressCb = _noop) -> None:
    """Resumable streaming download of mbdump.tar.bz2 for ``version``. Retries on
    transport/timeout errors, RESUMING from the bytes already on disk (Range) —
    the mirror intermittently drops the TLS handshake or stalls mid-stream, and
    a 7 GB transfer must survive a blip instead of restarting from zero."""
    import httpx
    url = f"{_MIRROR}/{version}/mbdump.tar.bz2"
    os.makedirs(_DATA, exist_ok=True)
    t0 = time.monotonic()
    for attempt in range(5):
        have = os.path.getsize(DUMP_PATH) if os.path.exists(DUMP_PATH) else 0
        headers = {"Range": f"bytes={have}-"} if have else {}
        try:
            with httpx.stream("GET", url, headers=headers,
                              timeout=httpx.Timeout(120.0, connect=30.0, read=120.0),
                              follow_redirects=True) as r:
                if r.status_code == 416:  # already fully downloaded
                    return
                resuming = r.status_code == 206
                r.raise_for_status()
                total = (have if resuming else 0) + int(r.headers.get("Content-Length", 0))
                done = have if resuming else 0
                a_start, a_bytes = time.monotonic(), 0
                with open(DUMP_PATH, "ab" if resuming else "wb") as fh:
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
                            raise httpx.ReadTimeout("MB download too slow — reconnecting")
                        if now - last > 0.5:  # throttle progress emits
                            last = now
                            pct = round(done / total * 100, 1) if total else 0
                            progress_cb({"phase": "downloading", "pct": pct,
                                         "downloaded_mb": round(done / 1e6),
                                         "total_mb": round(total / 1e6)})
            logger.info("download done in %.0fs (%.1f GB)", time.monotonic() - t0,
                        os.path.getsize(DUMP_PATH) / 1e9)
            return
        except (httpx.TransportError, httpx.TimeoutException) as e:
            logger.warning("MB download interrupted at %d bytes (try %d/5): %s",
                           have, attempt + 1, e)
            if attempt == 4:
                raise
            time.sleep(3)


def verify_md5(version: str) -> bool:
    """Compare the archive's MD5 against the mirror's MD5SUMS."""
    import httpx
    try:
        sums = httpx.get(f"{_MIRROR}/{version}/MD5SUMS", timeout=20.0).text
        expect = next((ln.split()[0] for ln in sums.splitlines()
                       if "mbdump.tar.bz2" in ln), None)
    except Exception:
        return True  # mirror unreachable → don't block on it
    if not expect:
        return True
    h = hashlib.md5()
    with open(DUMP_PATH, "rb") as f:
        for blk in iter(lambda: f.read(1 << 22), b""):
            h.update(blk)
    return h.hexdigest() == expect


# ── streaming load ───────────────────────────────────────────────────────────

def _ensure_schema(cur) -> None:
    for table, _ in TABLES:
        cur.execute("SELECT to_regclass(%s)", (table,))
        if cur.fetchone()[0] is None:
            raise RuntimeError(f"table {table} missing — apply mb_* DDL from 001_initial.sql")


def stream_load(progress_cb: ProgressCb = _noop) -> Dict[str, int]:
    """Decompress the archive once and pipe each needed member straight into
    ``COPY`` — no extracted files. Returns ``{table: rowcount_estimate}``."""
    from db_pool import get_conn
    if not os.path.exists(DUMP_PATH):
        raise RuntimeError(f"dump not found: {DUMP_PATH}")

    decomp = _decompressor()
    proc = None
    if decomp:
        proc = subprocess.Popen(decomp + [DUMP_PATH], stdout=subprocess.PIPE)
        tar = tarfile.open(fileobj=proc.stdout, mode="r|")
    else:
        tar = tarfile.open(DUMP_PATH, mode="r:bz2")  # Python bz2, single-thread

    counts: Dict[str, int] = {}
    t0 = time.monotonic()
    try:
        with get_conn() as conn:
            conn.autocommit = True
            with conn.cursor() as cur:
                _ensure_schema(cur)
            cum_w = 0.0  # byte-weighted progress accumulated over completed tables
            for member in tar:
                table = _MEMBER_TABLE.get(member.name)
                if not table:
                    continue
                fh = tar.extractfile(member)
                if fh is None:
                    continue
                w = _LOAD_WEIGHT.get(table, 1.0 / len(TABLES))
                size = member.size or 0
                # overall pct = (completed weight + this table's weight × byte frac)
                def _on_bytes(read_bytes, _w=w, _c=cum_w, _s=size, _t=table):
                    frac = (read_bytes / _s) if _s else 0.0
                    progress_cb({"phase": "loading", "table": _t,
                                 "pct": min(99, round((_c + _w * frac) * 100))})
                reader = _ProgressReader(fh, _on_bytes)
                with conn.cursor() as cur:
                    cur.execute(f"TRUNCATE {table}")
                    cur.copy_expert(f"COPY {table} FROM STDIN", reader)
                cum_w += w
                counts[table] = len(counts) + 1
                progress_cb({"phase": "loading", "table": table,
                             "pct": min(99, round(cum_w * 100))})
                logger.info("loaded %s (%d/%d)", table, len(counts), len(TABLES))
                if len(counts) == len(TABLES):
                    break  # everything we need is in — skip the (huge) tail
    finally:
        try:
            tar.close()
        except Exception:
            pass
        if proc:
            if proc.stdout:
                proc.stdout.close()
            proc.terminate()
            proc.wait()

    if len(counts) != len(TABLES):
        raise RuntimeError(f"loaded only {len(counts)}/{len(TABLES)} tables")

    progress_cb({"phase": "analyzing"})
    with get_conn() as conn:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute("ANALYZE " + ", ".join(t for t, _ in TABLES))
    logger.info("stream_load done in %.0fs", time.monotonic() - t0)
    return counts


# ── orchestrator ─────────────────────────────────────────────────────────────

def download_and_load(progress_cb: ProgressCb = _noop, force: bool = False) -> Dict:
    """Full update: pick newest version, download if needed, stream-load, stamp
    the VERSION marker. Returns ``{version, loaded, skipped_download}``."""
    progress_cb({"phase": "checking"})
    version = latest_version()
    if not version:
        raise RuntimeError("could not reach the MusicBrainz mirror")

    # "Up to date" keys on the VERSION marker + DB contents, NOT the archive —
    # which is deleted after each successful load — so a re-"Update" on the
    # current version does nothing instead of re-downloading 7 GB.
    if not force and loaded_version() == version and stats().get("loaded"):
        progress_cb({"phase": "done", "version": version})
        return {"version": version, "loaded": True, "up_to_date": True}

    download(version, progress_cb)  # resumes a partial; a complete archive → 416, instant
    stream_load(progress_cb)
    with open(VERSION_PATH, "w") as f:
        f.write(version)
    # Reclaim the ~7 GB archive — the data now lives in the DB, and a future
    # update re-downloads the (newer) version anyway.
    try:
        os.remove(DUMP_PATH)
    except OSError:
        pass
    progress_cb({"phase": "done", "version": version})
    return {"version": version, "loaded": True, "up_to_date": False}


# ── stats (cheap, for the UI) ────────────────────────────────────────────────

def stats() -> Dict:
    """reltuples estimates + on-disk size + loaded version. No count(*) scans."""
    from db_pool import db_query
    rows = db_query("""
        SELECT c.relname,
               GREATEST(c.reltuples, 0)::bigint AS est_rows,
               pg_total_relation_size(c.oid) AS bytes
        FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = 'public' AND c.relname = ANY(%(t)s)
    """, {"t": [t for t, _ in TABLES]})
    by = {r["relname"]: r for r in rows}
    loaded = sum(int(by.get(t, {}).get("est_rows", 0)) for t, _ in TABLES)
    size = sum(int(by.get(t, {}).get("bytes", 0)) for t, _ in TABLES)
    return {
        "loaded": bool(by) and int(by.get("mb_artist", {}).get("est_rows", 0)) > 0,
        "version": loaded_version(),
        "total_records": loaded,
        "size_bytes": size,
        "tables": len(by),
    }


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    ap = argparse.ArgumentParser(description="Download + load the MusicBrainz dump subset")
    ap.add_argument("--force", action="store_true", help="re-download even if current")
    ap.add_argument("--load-only", action="store_true", help="stream-load the existing archive")
    args = ap.parse_args()
    cb = lambda u: logger.info("progress %s", u)
    if args.load_only:
        stream_load(cb)
    else:
        print(download_and_load(cb, force=args.force))


if __name__ == "__main__":
    main()
