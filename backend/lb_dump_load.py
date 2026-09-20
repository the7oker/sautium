"""Download + aggregate the ListenBrainz statistics dump into the local
``lb_recording`` / ``lb_artist`` tables — the listening-statistics layer that
replaced Last.fm's per-track counts on 2026-09-20 (Last.fm's terms kept those
node-local; ListenBrainz data is CC0, so this layer travels between nodes as
signed per-artist slices, desktop/p2p/lb_slice_queries.py).

What the dump is: ``listenbrainz-statistics-dump-<ts>.tar.zst`` (~22 GB) under
``fullexport/listenbrainz-dump-<id>-<ts>-full/``, twice a month. Inside,
``lbdump/statistics/{stat}_{range}.jsonl`` — one JSON document per LB USER
and stat, its ``data`` array being that user's TOP 1000 recordings / artists.
Per-recording totals are NOT in any public dump (ListenBrainz keeps its
``popularity`` tables to itself), so this loader derives them: SUM of the
users' listen counts and COUNT of users, over ``recordings_all_time.jsonl``
and ``artists_all_time.jsonl`` only. Both are LOWER BOUNDS of the true
totals — a listen outside a user's top 1000 never reaches the dump — and
every consumer treats them as a rank, never as a figure to quote.

ECONOMICAL path, like mb_dump_load: the archive is streamed once through
``zstandard`` + ``tarfile`` and the items of the two members are COPYed
into UNLOGGED staging tables; the two aggregates are one ``GROUP BY`` each
(pushed into SQL, never a Python dict — 20 M+ distinct recordings). The tar
delivers artists first and recordings second, so reading stops after
``recordings_all_time.jsonl`` and the rest of the archive is never
decompressed. The new tables are built beside the old ones and SWAPPED in one
short transaction: readers are never blocked for the minutes the aggregation
takes and never see an empty table.

ONE completion marker: ``user_settings['listenbrainz.db_version']``, the only
value ever signed into a slice and the only "loaded here" truth (the MB
loader's file + key pair is what let its signed version and its serve gate
disagree). The archive name carries the version for download resume.
"""

import argparse
import json
import logging
import os
import re
import shutil
import tarfile
import time
from typing import Dict, List, Optional, Tuple

from dump_common import (ProgressCb, ProgressReader, download_resumable,
                         noop as _noop, rebuild_indexes, table_stats,
                         unlock_all, verify_sha256)

logger = logging.getLogger("lb_dump_load")

_HERE = os.path.dirname(os.path.abspath(__file__))
_DATA = os.environ.get("LB_DUMP_DIR") or os.path.normpath(os.path.join(_HERE, "..", "data", "lbdump"))

# Runtime disk gate (agent quote + update precondition). Measured on the
# master, 2026-09-20, dump 20260915-000002: archive 21.8 GB; staging 5.0 GB
# (29.6 M artist rows + 39.1 M recording rows from 84 850 users); loaded
# tables 0.85 GB (4.9 M recordings + 0.67 M artists). The archive, the
# staging tables and BOTH generations of the tables coexist at peak (the
# swap keeps the old ones until the new ones are indexed), on ONE volume in
# every shipped layout. Wall time: 17 min download at ~24 MB/s, 3 min
# checksum, 7 min stage + aggregate + swap.
ARCHIVE_GB = 22.0
STAGING_GB = 6.0
TABLES_GB = 2.0
MARGIN_GB = 2.0

_FULLEXPORT = "https://data.metabrainz.org/pub/musicbrainz/listenbrainz/fullexport"
_LB_UA = "Sautium/1.0 ( https://sautium.net )"   # ListenBrainz asks every client for a contact UA
_HEADERS = {"User-Agent": _LB_UA}

# The mirror is an nginx autoindex (no LATEST file): the newest export is the
# highest dump id; a directory still being uploaded lacks the archive or its
# checksum and is skipped for the previous one.
_DIR_RE = re.compile(r'href="(listenbrainz-dump-(\d+)-\d{8}-\d{6}-full)/?"')
_ARCHIVE_RE = re.compile(r'href="(listenbrainz-statistics-dump-(\d{8}-\d{6})\.tar\.zst)"')
_SHA_RE = re.compile(r'href="listenbrainz-statistics-dump-(\d{8}-\d{6})\.tar\.zst\.sha256"')
_HEX64 = re.compile(r"[0-9a-fA-F]{64}")
_UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")

# tar member basename → staging table. The dump tool writes stats in the
# order artists, recordings, releases, daily_activity, listening_activity,
# each over every range with all_time last — so these two close the useful
# part of the stream.
_MEMBERS = {"artists_all_time.jsonl": "lb_stage_artist",
            "recordings_all_time.jsonl": "lb_stage_recording"}
_LAST_MEMBER = "recordings_all_time.jsonl"

TABLES = ("lb_recording", "lb_artist")
_LEDGERS = ("lb_slice_fetches", "lb_slice_blobs", "lb_slice_requests")

# Cross-process "reload in flight" signal, held for the whole stage+aggregate
# +swap: the slice server try-locks it and answers 503 while held (a slice
# cut from a half-built table would be signed and closed for good on the
# requester), slice imports queue behind it. Never MB's 0x6D626C64.
LB_LOAD_LOCK_KEY = 0x6C626C64  # "lbld"
DB_VERSION_KEY = "listenbrainz.db_version"


def _archive_path(version: str) -> str:
    return os.path.join(_DATA, f"listenbrainz-statistics-dump-{version}.tar.zst")


# ── version ──────────────────────────────────────────────────────────────────

def parse_listing(html: str) -> List[Tuple[int, str]]:
    """(dump id, directory name) for every full export in the listing, newest first."""
    return sorted(((int(i), d) for d, i in _DIR_RE.findall(html)), reverse=True)


def parse_directory(html: str) -> Optional[Tuple[str, str]]:
    """(version, archive file name) when the directory holds the statistics
    archive AND its .sha256 — anything less is an upload in progress."""
    m = _ARCHIVE_RE.search(html)
    if not m:
        return None
    version = m.group(2)
    if version not in _SHA_RE.findall(html):
        return None
    return version, m.group(1)


def latest_version() -> Optional[Tuple[str, str]]:
    """(dump_version, archive url) of the newest complete statistics dump, or
    None if the mirror is unreachable. The version is the archive's own
    timestamp — the one that ends up in the DB marker and in every slice.
    Retried like the MB check: the mirror intermittently drops the TLS
    handshake, and one blip shouldn't fail the whole update."""
    import httpx
    for attempt in range(3):
        try:
            with httpx.Client(headers=_HEADERS, follow_redirects=True,
                              timeout=httpx.Timeout(30.0, connect=30.0)) as c:
                r = c.get(f"{_FULLEXPORT}/")
                r.raise_for_status()
                dirs = parse_listing(r.text)
                if not dirs:
                    raise RuntimeError("no full exports in the ListenBrainz listing")
                for _, d in dirs[:3]:
                    r = c.get(f"{_FULLEXPORT}/{d}/")
                    r.raise_for_status()
                    found = parse_directory(r.text)
                    if found:
                        version, archive = found
                        return version, f"{_FULLEXPORT}/{d}/{archive}"
                raise RuntimeError("no complete statistics dump in the newest ListenBrainz exports")
        except Exception as e:
            logger.warning("LB latest-version check failed (try %d/3): %s", attempt + 1, e)
            if attempt < 2:
                time.sleep(2)
    return None


def loaded_version() -> Optional[str]:
    """The dump version a FULL load finished on THIS database, else None."""
    from db_pool import db_query_one
    row = db_query_one("SELECT value FROM user_settings WHERE key = %(k)s", {"k": DB_VERSION_KEY})
    if not row or row["value"] in (None, ""):
        return None
    return str(row["value"])


# ── download + verify ────────────────────────────────────────────────────────

def expected_sha256(url: str) -> str:
    """The mirror's SHA-256 for the archive at ``url``. Fail closed: no
    checksum, no load — a mirror blip here costs a retry, a silently
    truncated 21 GB archive would cost a corrupt statistics layer."""
    import httpx
    r = httpx.get(url + ".sha256", headers=_HEADERS, timeout=30.0, follow_redirects=True)
    r.raise_for_status()
    tokens = r.text.split()
    if not tokens or not _HEX64.fullmatch(tokens[0]):
        raise RuntimeError(f"unparsable checksum file for {os.path.basename(url)}")
    return tokens[0].lower()


def download(version: str, url: str, progress_cb: ProgressCb = _noop) -> str:
    """Resumable download of the statistics archive, verified against the
    mirror's .sha256 before anything reads it. A mismatch deletes the file:
    the next attempt must start over, never resume into a corrupt archive."""
    path = _archive_path(version)
    expected = expected_sha256(url)   # before the 21 GB, not after
    download_resumable(url, path, progress_cb, label="ListenBrainz statistics",
                       headers=_HEADERS)
    size = os.path.getsize(path)
    progress_cb({"phase": "verifying", "pct": 0})
    try:
        verify_sha256(path, expected, on_bytes=lambda n: progress_cb(
            {"phase": "verifying", "pct": min(99, round(n / size * 100)) if size else 0}))
    except RuntimeError:
        os.remove(path)
        raise
    return path


# ── the JSONL → COPY reader ──────────────────────────────────────────────────

def recording_row(item: dict) -> Optional[bytes]:
    """One COPY text line for a user's recording item, or None for an
    unmapped listen (no recording MBID — the dump keeps those too)."""
    mbid = item.get("recording_mbid")
    if not isinstance(mbid, str) or not _UUID_RE.match(mbid):
        return None
    try:
        n = int(item.get("listen_count") or 0)
    except (TypeError, ValueError):
        return None
    if n <= 0:
        return None
    arts = item.get("artist_mbids") or []
    keep = [a.lower() for a in arts if isinstance(a, str) and _UUID_RE.match(a)]
    return f"{mbid.lower()}\t{n}\t{{{','.join(keep)}}}\n".encode()


def artist_row(item: dict) -> Optional[bytes]:
    mbid = item.get("artist_mbid")
    if not isinstance(mbid, str) or not _UUID_RE.match(mbid):
        return None
    try:
        n = int(item.get("listen_count") or 0)
    except (TypeError, ValueError):
        return None
    if n <= 0:
        return None
    return f"{mbid.lower()}\t{n}\n".encode()


_PARSERS = {"lb_stage_artist": artist_row, "lb_stage_recording": recording_row}


class ItemRows:
    """A COPY source over a statistics member: each JSONL line is one user's
    document, each of its ``data`` items becomes one staging row. A returned
    chunk always ends on a row boundary and may exceed ``size`` (one document
    is up to 1000 rows) — COPY accepts both, only an empty result ends it."""

    def __init__(self, fh, parse):
        self._fh, self._parse = fh, parse
        self.docs = 0
        self.seen = 0
        self.kept = 0

    def read(self, size: int = -1) -> bytes:
        out: list = []
        got = 0
        while size < 0 or got < size:
            line = self._fh.readline()
            if not line:
                break
            self.docs += 1
            doc = json.loads(line)
            for item in doc.get("data") or ():
                self.seen += 1
                row = self._parse(item)
                if row is None:
                    continue
                out.append(row)
                got += len(row)
                self.kept += 1
        return b"".join(out)


# ── streaming load ───────────────────────────────────────────────────────────

def _ensure_schema(cur) -> None:
    for table in TABLES + _LEDGERS:
        cur.execute("SELECT to_regclass(%s)", (table,))
        if cur.fetchone()[0] is None:
            raise RuntimeError(f"table {table} missing — apply the lb_* DDL (migration 020)")


def _stage(conn, path: str, progress_cb: ProgressCb) -> Dict[str, int]:
    """Stream the archive once; COPY the two useful members into the staging
    tables; stop at the end of the recordings member."""
    import zstandard
    size = os.path.getsize(path)
    kept: Dict[str, int] = {}
    with conn.cursor() as cur:
        cur.execute("DROP TABLE IF EXISTS lb_stage_recording, lb_stage_artist")
        cur.execute("CREATE UNLOGGED TABLE lb_stage_recording ("
                    "recording_mbid uuid NOT NULL, listen_count bigint NOT NULL, "
                    "artist_mbids uuid[] NOT NULL)")
        cur.execute("CREATE UNLOGGED TABLE lb_stage_artist ("
                    "artist_mbid uuid NOT NULL, listen_count bigint NOT NULL)")

    def _on_bytes(n: int) -> None:
        # Compressed bytes consumed — honest, the bar simply stops where the
        # useful members end and the phase flips to aggregating.
        progress_cb({"phase": "loading", "pct": min(99, round(n / size * 100)) if size else 0})

    with open(path, "rb") as raw:
        reader = ProgressReader(raw, _on_bytes)
        with zstandard.ZstdDecompressor().stream_reader(reader, read_across_frames=True) as z:
            with tarfile.open(fileobj=z, mode="r|") as tar:
                for member in tar:
                    name = os.path.basename(member.name)
                    table = _MEMBERS.get(name)
                    if table is None:
                        continue
                    fh = tar.extractfile(member)
                    if fh is None:
                        continue
                    rows = ItemRows(fh, _PARSERS[table])
                    t0 = time.monotonic()
                    with conn.cursor() as cur:
                        cur.copy_expert(f"COPY {table} FROM STDIN", rows)
                    kept[name] = rows.kept
                    logger.info("%s: %d users, %d of %d items kept in %.0fs", name,
                                rows.docs, rows.kept, rows.seen, time.monotonic() - t0)
                    if name == _LAST_MEMBER:
                        break
    missing = [m for m in _MEMBERS if m not in kept]
    if missing:
        raise RuntimeError(f"archive lacks {missing}")
    return kept


def _aggregate(conn, version: str, progress_cb: ProgressCb) -> None:
    """Two GROUP BYs into fresh tables, indexed, then swapped in for the old
    ones in one short transaction. ``min(artist_mbids)``: the credit array is
    a property of the recording's MBID mapping, identical in every user's
    document — any one of them is the one."""
    progress_cb({"phase": "aggregating"})
    with conn.cursor() as cur:
        cur.execute("BEGIN")
        cur.execute("SET LOCAL work_mem = '1GB'")
        cur.execute("DROP TABLE IF EXISTS lb_recording_new, lb_artist_new")
        cur.execute("CREATE TABLE lb_recording_new "
                    "(LIKE lb_recording INCLUDING DEFAULTS INCLUDING CONSTRAINTS)")
        cur.execute("""
            INSERT INTO lb_recording_new
                   (recording_mbid, listen_count, user_count, artist_mbids, dump_version)
            SELECT recording_mbid, SUM(listen_count), COUNT(*)::int, MIN(artist_mbids), %s
              FROM lb_stage_recording
             GROUP BY recording_mbid""", (version,))
        cur.execute("CREATE TABLE lb_artist_new "
                    "(LIKE lb_artist INCLUDING DEFAULTS INCLUDING CONSTRAINTS)")
        cur.execute("""
            INSERT INTO lb_artist_new (artist_mbid, listen_count, user_count, dump_version)
            SELECT artist_mbid, SUM(listen_count), COUNT(*)::int, %s
              FROM lb_stage_artist
             GROUP BY artist_mbid""", (version,))
        cur.execute("DROP TABLE lb_stage_recording, lb_stage_artist")
        cur.execute("COMMIT")

    rebuild_indexes(conn, {
        "lb_recording_new_pkey": "ALTER TABLE lb_recording_new ADD PRIMARY KEY (recording_mbid)",
        "idx_lb_recording_artists_new": "CREATE INDEX idx_lb_recording_artists_new "
                                        "ON lb_recording_new USING gin (artist_mbids)",
        "lb_artist_new_pkey": "ALTER TABLE lb_artist_new ADD PRIMARY KEY (artist_mbid)",
    }, lambda f: progress_cb({"phase": "indexing", "pct": min(99, round(f * 100))}))

    with conn.cursor() as cur:
        cur.execute("BEGIN")
        cur.execute("DROP TABLE lb_recording")
        cur.execute("ALTER TABLE lb_recording_new RENAME TO lb_recording")
        cur.execute("ALTER INDEX lb_recording_new_pkey RENAME TO lb_recording_pkey")
        cur.execute("ALTER INDEX idx_lb_recording_artists_new RENAME TO idx_lb_recording_artists")
        cur.execute("DROP TABLE lb_artist")
        cur.execute("ALTER TABLE lb_artist_new RENAME TO lb_artist")
        cur.execute("ALTER INDEX lb_artist_new_pkey RENAME TO lb_artist_pkey")
        cur.execute("COMMIT")


def stream_load(version: str, progress_cb: ProgressCb = _noop) -> Dict[str, int]:
    """Stage + aggregate + swap under the loader's advisory lock; ANALYZE
    after. Returns the kept-item counts per member."""
    from db_pool import get_conn
    path = _archive_path(version)
    if not os.path.exists(path):
        raise RuntimeError(f"dump not found: {path}")
    t0 = time.monotonic()
    with get_conn() as conn:
        with conn.cursor() as cur:
            _ensure_schema(cur)
            # Session-level lock on a POOLED connection — released explicitly
            # (unlock_all in the finally), never on connection close.
            cur.execute("SELECT pg_advisory_lock(%s)", (LB_LOAD_LOCK_KEY,))
        try:
            kept = _stage(conn, path, progress_cb)
            _aggregate(conn, version, progress_cb)
        finally:
            unlock_all(conn)
        progress_cb({"phase": "analyzing"})
        with conn.cursor() as cur:
            for i, t in enumerate(TABLES):
                progress_cb({"phase": "analyzing", "table": t,
                             "pct": round(i / len(TABLES) * 100)})
                cur.execute(f"ANALYZE {t}")
    logger.info("stream_load %s done in %.0fs", version, time.monotonic() - t0)
    return kept


# ── orchestrator ─────────────────────────────────────────────────────────────

def _mark_loaded(version: str) -> None:
    """One statement: the completion marker, and the slice ledgers cleared —
    a node that holds the whole dump has nothing to ask for, and its cached
    blobs are at an old version (rebuilt lazily by the slice server)."""
    from db_pool import get_conn
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("BEGIN")
            cur.execute("INSERT INTO user_settings (key, value) VALUES (%s, %s::jsonb) "
                        "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
                        (DB_VERSION_KEY, json.dumps(version)))
            cur.execute("DELETE FROM lb_slice_fetches")
            cur.execute("DELETE FROM lb_slice_requests")
            cur.execute("TRUNCATE lb_slice_blobs")
            cur.execute("COMMIT")


def download_and_load(progress_cb: ProgressCb = _noop, force: bool = False) -> Dict:
    """Full update: newest complete dump, download if needed, stage +
    aggregate + swap, stamp the marker. Returns ``{version, loaded, up_to_date}``."""
    from db_pool import db_execute
    progress_cb({"phase": "checking"})
    latest = latest_version()
    if not latest:
        raise RuntimeError("could not reach the ListenBrainz mirror")
    version, url = latest
    if not force and loaded_version() == version and stats().get("loaded"):
        progress_cb({"phase": "done", "version": version})
        return {"version": version, "loaded": True, "up_to_date": True}
    path = download(version, url, progress_cb)  # resumes a partial; complete → 416, instant
    kept = stream_load(version, progress_cb)
    _mark_loaded(version)
    # Reclaim the archive — the data now lives in the DB, and a future update
    # downloads the (newer) version anyway.
    try:
        os.remove(path)
    except OSError:
        pass
    # The peer surface re-reads the marker; the launcher's P2PManager announces
    # the lbdump capability; the Streaming library block shows the version.
    db_execute("NOTIFY sautium_lb_sources")
    progress_cb({"phase": "done", "version": version})
    return {"version": version, "loaded": True, "up_to_date": False, "kept": kept}


# ── stats (cheap, for the UI) ────────────────────────────────────────────────

def stats() -> Dict:
    """reltuples estimates + on-disk size + loaded version. No count(*) scans.
    ``loaded`` needs the marker too: a slice node's lb_recording has rows
    (its artists' slices) and no dump."""
    by = table_stats(TABLES)
    version = loaded_version()
    recordings = by.get("lb_recording", {}).get("est_rows", 0)
    return {
        "loaded": version is not None and recordings > 0,
        "version": version,
        "total_records": sum(v["est_rows"] for v in by.values()),
        "size_bytes": sum(v["bytes"] for v in by.values()),
        "tables": len(by),
        "catalogue": {
            "recordings": recordings,
            "artists": by.get("lb_artist", {}).get("est_rows", 0),
        },
    }


def disk_budget() -> Dict:
    """Can this volume take the dump? Free space is measured at the archive
    dir, which shares the volume with the Postgres datadir in every shipped
    layout. Peak = archive + staging + the new tables (built beside the old
    ones until the swap); bytes already on disk count toward the download."""
    try:
        free_gb = shutil.disk_usage(_DATA if os.path.isdir(_DATA)
                                    else os.path.dirname(_DATA)).free / 1e9
    except OSError:
        free_gb = 0.0
    have_archive = 0.0
    if os.path.isdir(_DATA):
        for name in os.listdir(_DATA):
            if name.endswith(".tar.zst"):
                try:
                    have_archive += os.path.getsize(os.path.join(_DATA, name)) / 1e9
                except OSError:
                    pass
    download_gb = max(0.0, ARCHIVE_GB - have_archive)
    if download_gb < 0.3:
        download_gb = 0.0   # ARCHIVE_GB is nominal; a complete archive reads as "nothing left"
    required_gb = download_gb + STAGING_GB + TABLES_GB + MARGIN_GB
    return {
        "download_gb": round(download_gb, 1),
        "required_gb": round(required_gb, 1),
        "free_gb": round(free_gb, 1),
        "can_fit": free_gb >= required_gb,
    }


def delete_dump() -> Dict:
    """Remove the local statistics: TRUNCATE the lb_* tables, the marker and
    the archives. The reverse of the wizard's opt-in. The slice ledgers go
    too — the node is a slice node again and its cycle refills what its
    artists need (NOTIFY sautium_lb_pending wakes it)."""
    from db_pool import db_execute, get_conn
    freed = stats().get("size_bytes", 0)
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT pg_try_advisory_lock(%s)", (LB_LOAD_LOCK_KEY,))
            if not cur.fetchone()[0]:
                raise RuntimeError("a dump load is running")
            try:
                cur.execute("BEGIN")
                cur.execute("TRUNCATE lb_recording, lb_artist, lb_slice_blobs")
                cur.execute("DELETE FROM lb_slice_fetches")
                cur.execute("DELETE FROM lb_slice_requests")
                cur.execute("DELETE FROM user_settings WHERE key = %s", (DB_VERSION_KEY,))
                cur.execute("COMMIT")
            finally:
                cur.execute("SELECT pg_advisory_unlock(%s)", (LB_LOAD_LOCK_KEY,))
    for name in os.listdir(_DATA) if os.path.isdir(_DATA) else []:
        if name.endswith(".tar.zst"):
            try:
                os.remove(os.path.join(_DATA, name))
            except OSError:
                pass
    db_execute("NOTIFY sautium_lb_sources")
    db_execute("NOTIFY sautium_lb_pending")
    logger.info("LB statistics deleted (~%.1f GB of tables freed)", freed / 1e9)
    return {"deleted": True, "freed_bytes": freed}


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    ap = argparse.ArgumentParser(description="Download + aggregate the ListenBrainz statistics dump")
    ap.add_argument("--force", action="store_true", help="re-download even if current")
    ap.add_argument("--load-only", metavar="VERSION",
                    help="stage + aggregate the archive already on disk for VERSION")
    args = ap.parse_args()
    cb = lambda u: logger.info("progress %s", u)
    if args.load_only:
        print(stream_load(args.load_only, cb))
        _mark_loaded(args.load_only)
    else:
        print(download_and_load(cb, force=args.force))


if __name__ == "__main__":
    main()
