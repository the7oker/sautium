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

FAST load path: indexes + PK/UNIQUE constraints are dropped before each
table's COPY and rebuilt right after (see the index drop/rebuild section), and
TRUNCATE+COPY share one transaction so ``wal_level=minimal`` skips WAL for the
bulk write. A DDL snapshot persisted next to the archive makes a crash at any
point recoverable: the next run merges it with the live catalogs and rebuilds.

Column order in the headerless dump TSV == MB ``CreateTables.sql`` order; the
``mb_*`` definitions (001_initial.sql) mirror it, so a default ``COPY`` round-
trips it and a schema drift fails loudly on field count.
"""

import argparse
import hashlib
import logging
import os
import re
import shutil
import subprocess
import tarfile
import time
from typing import Callable, Dict, Optional

from dump_common import (ProgressCb, ProgressReader, collect_index_ddl,
                         download_resumable, drop_indexes, noop as _noop,
                         rebuild_indexes, unlock_all)

logger = logging.getLogger("mb_dump_load")

_HERE = os.path.dirname(os.path.abspath(__file__))
_DATA = os.environ.get("MB_DUMP_DIR") or os.path.normpath(os.path.join(_HERE, "..", "data", "mbdump"))
DUMP_PATH = os.path.join(_DATA, "mbdump.tar.bz2")
VERSION_PATH = os.path.join(_DATA, "VERSION")

# Runtime disk gate (agent quote + update precondition). Measured 2026-08:
# core archive ~7 GB compressed, loaded mb_* tables ~21 GB — in both shipped
# layouts (Docker ./data/*, launcher data_dir/*) they live on ONE volume and
# coexist at stream-load peak. Nominal constants, not a mirror HEAD: the
# budget must answer in milliseconds on the mb_resolve no-dump path.
ARCHIVE_GB = 7.5
TABLES_GB = 21.0
MARGIN_GB = 2.0

_MIRROR = "https://data.metabrainz.org/pub/musicbrainz/data/fullexport"
_MB_WS = "https://musicbrainz.org/ws/2"   # genre vocabulary — not in the dump archives
_MB_UA = "Sautium/1.0 ( https://sautium.net )"

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
    ("mb_release_country", "release_country"),
    ("mb_release_unknown_country", "release_unknown_country"),
    ("mb_medium_format", "medium_format"),
    ("mb_medium", "medium"),
    ("mb_recording", "recording"),
    ("mb_track", "track"),
    ("mb_release_label", "release_label"),
    # URL relationships, Bandcamp only — filtered at COPY time (_ROW_FILTERS).
    # The two link members precede url in the core archive, so they are
    # spooled until the surviving url ids are known (stream_load).
    ("mb_url", "url"),
    ("mb_l_artist_url", "l_artist_url"),
    ("mb_l_release_url", "l_release_url"),
    # Folksonomy tags ship in the smaller mbdump-derived archive, not core
    # mbdump (see _ARCHIVE). The curated genre vocabulary (mb_genre) is NOT in
    # the streamed archives — it lives in the 7 GB core, not worth re-fetching
    # for a ~2k-row static list, so it's populated from the MB API
    # (load_genre_list). Genres are the curated subset of tags — overlap is
    # computed on tag.name ∈ genre.name, never on raw folksonomy tags.
    ("mb_tag", "tag"),
    ("mb_artist_tag", "artist_tag"),
    ("mb_release_group_tag", "release_group_tag"),
]
_MEMBER_TABLE = {f"mbdump/{m}": t for t, m in TABLES}

# The full export is split across archives; each table lives in exactly one.
# Tables not listed default to the 7 GB core "mbdump"; the *_tag/genre tables are
# in the ~480 MB "mbdump-derived", so they (re)load without re-fetching the core.
_DEFAULT_ARCHIVE = "mbdump"
_ARCHIVE = {
    "mb_tag": "mbdump-derived",
    "mb_artist_tag": "mbdump-derived",
    "mb_release_group_tag": "mbdump-derived",
}


def _archive_of(table: str) -> str:
    return _ARCHIVE.get(table, _DEFAULT_ARCHIVE)


def _archive_tables(archive: str) -> list:
    return [(t, m) for t, m in TABLES if _archive_of(t) == archive]


def _archives() -> list:
    """Distinct archives in load order (core first, then derived)."""
    out: list = []
    for t, _ in TABLES:
        a = _archive_of(t)
        if a not in out:
            out.append(a)
    return out


def _dump_path(archive: str) -> str:
    return os.path.join(_DATA, f"{archive}.tar.bz2")

# Approx fraction of total load each table represents (by TSV size) — so the bar
# is byte-weighted (smooth) instead of one even 1/10 step per table (which jumps
# unevenly: a 13 MB table and a 750 MB table are NOT each 10% of the work).
# Stable across dump versions (tables grow together); sums to ~1.0.
_LOAD_WEIGHT = {
    "mb_track": 0.32, "mb_recording": 0.25, "mb_release": 0.08,
    "mb_artist": 0.07, "mb_release_group": 0.05, "mb_artist_credit_name": 0.04,
    "mb_artist_credit": 0.03, "mb_medium": 0.03, "mb_release_label": 0.02,
    # The url members are read whole and filtered line by line — the weight
    # is the walk, not the few rows that survive it.
    "mb_url": 0.04, "mb_l_artist_url": 0.02, "mb_l_release_url": 0.02,
    "mb_artist_alias": 0.02, "mb_area": 0.01,
    "mb_release_country": 0.01, "mb_release_unknown_country": 0.0,
    "mb_release_group_secondary_type_join": 0.0,
    "mb_release_group_primary_type": 0.0, "mb_release_group_secondary_type": 0.0,
    "mb_medium_format": 0.0,
    # derived archive — weights are per-archive (each stream_load sums only its
    # own tables), so these sum to ~1 on their own; the *_tag tables dominate.
    "mb_release_group_tag": 0.65, "mb_artist_tag": 0.30, "mb_tag": 0.05,
}

# progress_cb(update: dict). Phases: checking|downloading|loading|analyzing|done|error
# — the vocabulary and the reader live in dump_common (shared with lb_dump_load).


# ── row filters (COPY text) ─────────────────────────────────────────────────
# A table listed in _ROW_FILTERS keeps only the rows its predicate admits,
# decided line by line on the dump's COPY text (tab-separated; a raw tab or
# newline never occurs inside a field, both are escaped). mb_url keeps
# Bandcamp pages — the one store the Buy affordance targets
# (docs/design/PHANTOM-DISCOVERY.md D5) — which cuts MB's ~22M urls to
# ~0.8M. The two link tables keep the rows whose url survived, so they need
# the surviving ids first: _FILTER_AFTER names that dependency, and
# stream_load spools such a member to disk when the archive delivers it
# before its dependency (in the core archive l_artist_url and l_release_url
# both precede url).

# daily.bandcamp.com is editorial; every other subdomain is an artist's or a
# label's shop. Bare bandcamp.com is the site itself, never a page to buy from.
_BANDCAMP_URL = re.compile(rb"^https?://(?!daily\.)[a-z0-9-]+\.bandcamp\.com/",
                           re.IGNORECASE)

_FILTER_AFTER = {"mb_l_artist_url": "mb_url", "mb_l_release_url": "mb_url"}


def _row_filters(url_ids: set) -> Dict[str, Callable[[list], bool]]:
    """Per-table predicates over the split COPY line. `url_ids` is filled by
    the mb_url predicate and read by the link predicates — the loader
    guarantees that order."""
    def keep_url(fields: list) -> bool:
        if _BANDCAMP_URL.match(fields[2]):
            url_ids.add(int(fields[0]))
            return True
        return False

    def keep_link(fields: list) -> bool:
        return int(fields[3]) in url_ids

    return {"mb_url": keep_url,
            "mb_l_artist_url": keep_link, "mb_l_release_url": keep_link}


class _FilteredReader:
    """A COPY source that hands psycopg2 only the lines `keep` admits. Reads
    the member line by line, so a returned chunk always ends on a row
    boundary; `read` may return fewer than `size` bytes, which COPY accepts
    (only an empty result means end of data)."""

    def __init__(self, fh, keep: Callable[[list], bool]):
        self._fh, self._keep = fh, keep
        self.seen = 0
        self.kept = 0

    def read(self, size: int = -1) -> bytes:
        out: list = []
        got = 0
        while size < 0 or got < size:
            line = self._fh.readline()
            if not line:
                break
            self.seen += 1
            if self._keep(line.rstrip(b"\n").split(b"\t")):
                out.append(line)
                got += len(line)
                self.kept += 1
        return b"".join(out)


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

def download(version: str, archive: str = _DEFAULT_ARCHIVE,
             progress_cb: ProgressCb = _noop) -> None:
    """Resumable streaming download of ``{archive}.tar.bz2`` for ``version``
    (dump_common.download_resumable: Range resume, stuck-slow reconnect)."""
    download_resumable(f"{_MIRROR}/{version}/{archive}.tar.bz2", _dump_path(archive),
                       progress_cb, label=f"MB {archive}")


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

# Cross-process "reload in flight" signal: stream_load holds this advisory
# lock for the whole TRUNCATE+COPY loop; the discography reconcile try-locks
# it and skips while held (half-loaded mb_* tables would read as an empty
# discography and strip phantom shelves). DB-level so it works no matter
# which process (backend thread, docker exec, launcher) runs the load.
MB_LOAD_LOCK_KEY = 0x6D626C64  # "mbld"


def _ensure_schema(cur, tables) -> None:
    for table, _ in tables:
        cur.execute("SELECT to_regclass(%s)", (table,))
        if cur.fetchone()[0] is None:
            raise RuntimeError(f"table {table} missing — apply mb_* DDL from 001_initial.sql")


# ── index drop/rebuild around COPY (dump_common) ─────────────────────────────
# COPY into indexed tables maintains every index row-by-row — on this dump
# that is ~7 GB of btree+GIN (mb_track alone: 3.2 GB across 4 indexes, and the
# trigram GINs on mb_artist/mb_release_group are the worst per-row cost).
# Dropping indexes + PK/UNIQUE constraints first and rebuilding after the COPY
# replaces incremental maintenance with sorted bottom-up builds (parallel on
# PG18, GIN included) — several times faster end-to-end and yields compact
# indexes. Readers seq-scan meanwhile; the advisory lock already serializes
# every mb_* writer (slice imports block on it, reconcile skips).

# Fraction of a table's progress weight spent in COPY; the rest is the rebuild.
_COPY_FRAC = 0.75


def _saved_ddl_path(archive: str) -> str:
    return os.path.join(_DATA, f"indexes_{archive}.json")


def stream_load(archive: str = _DEFAULT_ARCHIVE,
                progress_cb: ProgressCb = _noop,
                tables: Optional[list] = None) -> Dict[str, int]:
    """Decompress ``{archive}.tar.bz2`` once and pipe each member belonging to
    that archive straight into ``COPY`` — no extracted files, indexes dropped
    for the COPY and rebuilt per table. ``tables`` narrows the pass to a
    subset of the archive's (table, member) pairs — how tables added to
    TABLES after a dump landed are loaded without redoing the rest.
    Returns ``{table: load_order}``."""
    from db_pool import get_conn
    path = _dump_path(archive)
    if not os.path.exists(path):
        raise RuntimeError(f"dump not found: {path}")

    tables = tables or _archive_tables(archive)
    member_table = {f"mbdump/{m}": t for t, m in tables}
    expected = len(tables)
    in_pass = {t for t, _ in tables}
    url_ids: set = set()
    filters = _row_filters(url_ids)
    # Weights are per pass: a subset load must still walk the bar to 100.
    total_w = sum(_LOAD_WEIGHT.get(t, 0.01) for t, _ in tables) or 1.0

    decomp = _decompressor()
    proc = None
    if decomp:
        proc = subprocess.Popen(decomp + [path], stdout=subprocess.PIPE)
        tar = tarfile.open(fileobj=proc.stdout, mode="r|")
    else:
        tar = tarfile.open(path, mode="r:bz2")  # Python bz2, single-thread

    counts: Dict[str, int] = {}
    t0 = time.monotonic()
    try:
        with get_conn() as conn:
            conn.autocommit = True
            with conn.cursor() as cur:
                _ensure_schema(cur, tables)
                # Session-level lock on a POOLED connection — must be
                # released explicitly (finally below), not on conn close.
                cur.execute("SELECT pg_advisory_lock(%s)", (MB_LOAD_LOCK_KEY,))
            try:
                ddl = collect_index_ddl(conn, [t for t, _ in tables], _saved_ddl_path(archive))
                cum_w = 0.0  # byte-weighted progress accumulated over completed tables

                def _load_table(table: str, fh, size: int) -> None:
                    nonlocal cum_w
                    w = _LOAD_WEIGHT.get(table, 0.01) / total_w
                    # overall pct = (completed weight + this table's weight × byte frac)
                    def _on_bytes(read_bytes, _w=w, _c=cum_w, _s=size, _t=table):
                        frac = (read_bytes / _s) if _s else 0.0
                        progress_cb({"phase": "loading", "table": _t,
                                     "pct": min(99, round((_c + _w * _COPY_FRAC * frac) * 100))})
                    reader = ProgressReader(fh, _on_bytes)
                    keep = filters.get(table)
                    if keep:
                        reader = _FilteredReader(reader, keep)
                    with conn.cursor() as cur:
                        drop_indexes(cur, table, ddl[table])
                        # One transaction for TRUNCATE+COPY: under
                        # wal_level=minimal the COPY then skips WAL entirely,
                        # and readers never see a half-loaded table.
                        cur.execute("BEGIN")
                        cur.execute(f"TRUNCATE {table}")
                        cur.copy_expert(f"COPY {table} FROM STDIN", reader)
                        cur.execute("COMMIT")
                    if keep:
                        logger.info("%s: kept %d of %d rows", table,
                                    reader.kept, reader.seen)
                    def _on_frac(frac, _w=w, _c=cum_w, _t=table):
                        part = _COPY_FRAC + (1 - _COPY_FRAC) * min(frac, 1.0)
                        progress_cb({"phase": "indexing", "table": _t,
                                     "pct": min(99, round((_c + _w * part) * 100))})
                    rebuild_indexes(conn, ddl[table], _on_frac)
                    cum_w += w
                    counts[table] = len(counts) + 1
                    progress_cb({"phase": "loading", "table": table,
                                 "pct": min(99, round(cum_w * 100))})
                    logger.info("loaded %s (%d/%d)", table, len(counts), expected)

                spooled: list = []   # (table, path, size) parked until their dependency lands
                for member in tar:
                    table = member_table.get(member.name)
                    if not table:
                        continue
                    fh = tar.extractfile(member)
                    if fh is None:
                        continue
                    dep = _FILTER_AFTER.get(table)
                    if dep and dep in in_pass and dep not in counts:
                        # Its filter needs ids the archive has not delivered
                        # yet — park the raw member on disk, load it after.
                        spath = os.path.join(_DATA, f"spool_{table}.tsv")
                        progress_cb({"phase": "loading", "table": table,
                                     "pct": min(99, round(cum_w * 100))})
                        with open(spath, "wb") as out:
                            shutil.copyfileobj(fh, out, 1 << 20)
                        spooled.append((table, spath, member.size or 0))
                        logger.info("spooled %s (%d MB) until %s is loaded",
                                    table, (member.size or 0) >> 20, dep)
                    else:
                        if dep and dep not in in_pass and not url_ids:
                            # The dependency was loaded in an earlier pass —
                            # its surviving ids come from the table itself.
                            with conn.cursor() as cur:
                                cur.execute(f"SELECT id FROM {dep}")
                                url_ids.update(r[0] for r in cur.fetchall())
                        _load_table(table, fh, member.size or 0)
                    if len(counts) + len(spooled) == expected:
                        break  # everything in this archive is in — skip the tail
                for table, spath, size in spooled:
                    with open(spath, "rb") as fh:
                        _load_table(table, fh, size)
                    os.remove(spath)
            finally:
                unlock_all(conn)
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

    if len(counts) != expected:
        raise RuntimeError(f"loaded only {len(counts)}/{expected} tables from {archive}")

    progress_cb({"phase": "analyzing"})
    with get_conn() as conn:
        conn.autocommit = True
        with conn.cursor() as cur:
            # Per table (not one statement) so multi-minute ANALYZE over 21 GB
            # moves the bar instead of freezing at the loading pct.
            for i, (t, _) in enumerate(tables):
                progress_cb({"phase": "analyzing", "table": t,
                             "pct": round(i / len(tables) * 100)})
                cur.execute(f"ANALYZE {t}")
    # Every table is loaded and re-indexed — the crash-file has served its
    # purpose (a future load re-snapshots the live catalogs).
    try:
        os.remove(_saved_ddl_path(archive))
    except FileNotFoundError:
        pass
    logger.info("stream_load %s done in %.0fs", archive, time.monotonic() - t0)
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
        missing = missing_tables()
        if not missing:
            progress_cb({"phase": "done", "version": version})
            return {"version": version, "loaded": True, "up_to_date": True}
        # Tables added to TABLES since this dump landed: fetch the same
        # version's archives again and load just those — the rest of the
        # dump is untouched, so none of the post-load refreshes below apply.
        # (A dump older than the mirror's newest takes the full path, which
        # loads everything, the new tables included.)
        logger.info("MB dump %s is current but lacks %s — loading those",
                    version, missing)
        for archive in _archives():
            subset = [(t, m) for t, m in _archive_tables(archive) if t in missing]
            if not subset:
                continue
            download(version, archive, progress_cb)
            stream_load(archive, progress_cb, tables=subset)
            try:
                os.remove(_dump_path(archive))
            except OSError:
                pass
        # The node holds every wire table again → it is a dump source again.
        from db_pool import db_execute
        db_execute("NOTIFY sautium_mb_sources")
        progress_cb({"phase": "done", "version": version})
        return {"version": version, "loaded": True, "up_to_date": False,
                "added": missing}

    for archive in _archives():
        download(version, archive, progress_cb)  # resumes a partial; complete → 416, instant
        stream_load(archive, progress_cb)
        # Reclaim the archive — the data now lives in the DB, and a future
        # update re-downloads the (newer) version anyway.
        try:
            os.remove(_dump_path(archive))
        except OSError:
            pass
    # Genres aren't in the dump archives — refresh the curated vocabulary too so a
    # full update keeps mb_genre in sync with the dumped tags. Non-fatal: a dump
    # reload shouldn't fail just because the MB API blipped.
    try:
        load_genre_list(progress_cb)
    except Exception as e:
        logger.warning("genre vocabulary refresh failed (kept previous): %s", e)
    # Album genres (source='mb') derive from release_group_tag ∩ genre — rebuild
    # so a dump reload keeps them in sync. Non-fatal, like the genre refresh.
    try:
        from canon.genres import refresh_album_mb_genres
        refresh_album_mb_genres(progress_cb)
    except Exception as e:
        logger.warning("album mb-genre refresh failed (kept previous): %s", e)
    # Fresh MB data invalidates missing-album discovery: un-stamp so the
    # background reconcile re-derives every canonized artist's phantom
    # shelf against the new dump (incl. release years once
    # mb_release_country is populated).
    from db_pool import db_execute
    db_execute("UPDATE artists SET last_album_sync = NULL")
    with open(VERSION_PATH, "w") as f:
        f.write(version)
    # Completion marker IN THE DB — the per-DSN truth that a FULL load
    # finished here. The VERSION file alone can't carry that: it is shared
    # between runtimes on a dev host, and slice imports populate mb_artist
    # on dump-less nodes (see mb_slice_queries.DB_VERSION_KEY).
    import json as _json
    db_execute(
        "INSERT INTO user_settings (key, value) VALUES "
        "('musicbrainz.db_version', %s::jsonb) "
        "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
        (_json.dumps(version),))
    # Wake the mb-capability listener: the Discovery chip flips to the
    # enabled 'local' state the moment the full load lands.
    db_execute("NOTIFY sautium_mb_sources")
    progress_cb({"phase": "done", "version": version})
    return {"version": version, "loaded": True, "up_to_date": False}


def load_archive(archive: str, version: Optional[str] = None,
                 progress_cb: ProgressCb = _noop) -> Dict:
    """Download + stream-load ONE archive at ``version`` (default: the currently
    loaded core version, so a derived add lines up with the core's entity ids).
    Removes the tarball after; leaves the VERSION marker alone (the core owns it).
    Used to add the *_tag/genre tables without re-fetching the 7 GB core."""
    version = version or loaded_version() or latest_version()
    if not version:
        raise RuntimeError("could not determine an MB version to load")
    progress_cb({"phase": "checking", "version": version})
    download(version, archive, progress_cb)
    counts = stream_load(archive, progress_cb)
    # The derived archive carries release_group_tag → rebuild album mb-genres.
    if archive == "mbdump-derived":
        try:
            from canon.genres import refresh_album_mb_genres
            refresh_album_mb_genres(progress_cb)
        except Exception as e:
            logger.warning("album mb-genre refresh failed (kept previous): %s", e)
    try:
        os.remove(_dump_path(archive))
    except OSError:
        pass
    progress_cb({"phase": "done", "version": version})
    return {"version": version, "archive": archive, "loaded": counts}


def load_genre_list(progress_cb: ProgressCb = _noop) -> int:
    """Populate mb_genre from MusicBrainz's curated genre vocabulary
    (ws/2/genre/all). Genres aren't in the streamed dump archives (they live in
    the 7 GB core, not worth re-fetching for a ~2k-row static list). Names only —
    that's all the genre filter needs (tag.name ∈ genre.name). Idempotent."""
    import httpx
    from psycopg2.extras import execute_values
    from db_pool import get_conn
    r = httpx.get(f"{_MB_WS}/genre/all?fmt=txt", headers={"User-Agent": _MB_UA},
                  timeout=120.0, follow_redirects=True)
    r.raise_for_status()
    names = sorted({ln.strip() for ln in r.text.splitlines() if ln.strip()})
    if not names:
        raise RuntimeError("MB genre list came back empty")
    with get_conn() as conn:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute("TRUNCATE mb_genre")
            execute_values(cur, "INSERT INTO mb_genre (name) VALUES %s",
                           [(n,) for n in names])
            cur.execute("ANALYZE mb_genre")
    logger.info("loaded %d genres from MB", len(names))
    progress_cb({"phase": "genres", "count": len(names)})
    return len(names)



# ── stats (cheap, for the UI) ────────────────────────────────────────────────

def stats() -> Dict:
    """reltuples estimates + on-disk size + loaded version. No count(*) scans."""
    from db_pool import db_query, db_query_one
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
    # Tables with no rows behind a COMPLETED load here — the ones added to
    # TABLES after this dump landed (the url tables, 2026-09-17). Keyed on the
    # in-DB marker, not the VERSION file: on a dev host the file is shared
    # with a launcher whose own PG holds no dump, and every table would read
    # as missing there. The loader ANALYZEs after COPY, so reltuples is exact.
    full_load_here = db_query_one(
        "SELECT 1 AS x FROM user_settings WHERE key = 'musicbrainz.db_version'") is not None
    missing = ([t for t, _ in TABLES if int(by.get(t, {}).get("est_rows", 0)) <= 0]
               if full_load_here else [])
    return {
        "loaded": bool(by) and int(by.get("mb_artist", {}).get("est_rows", 0)) > 0,
        "version": loaded_version(),
        "total_records": loaded,
        "size_bytes": size,
        "tables": len(by),
        "missing_tables": missing,
        # What the catalogue actually holds, for the UI to name. Same free
        # reltuples read as the totals above — the loader ANALYZEs each table
        # after the bulk COPY and nothing writes to them afterwards, so these
        # match count(*) exactly (verified: 4,386,143 / 2,921,591).
        "catalogue": {
            "albums":     int(by.get("mb_release_group", {}).get("est_rows", 0)),
            "artists":    int(by.get("mb_artist", {}).get("est_rows", 0)),
            "recordings": int(by.get("mb_recording", {}).get("est_rows", 0)),
        },
    }


def missing_tables() -> list:
    """The TABLES this node's completed dump lacks — see stats()."""
    return list(stats().get("missing_tables") or [])


def disk_budget() -> Dict:
    """Can this volume take the dump? Free space is measured at the archive
    dir, which shares the volume with the Postgres datadir in every shipped
    layout — the two big consumers.

    Fresh install peak = archive + loaded tables coexisting through
    stream-load. With a dump already loaded the tables TRUNCATE-then-COPY
    into space they already occupy, so only the (re)download must fit; bytes
    already on disk count toward the download (resume)."""
    try:
        free_gb = shutil.disk_usage(_DATA if os.path.isdir(_DATA)
                                    else os.path.dirname(_DATA)).free / 1e9
    except OSError:
        free_gb = 0.0
    have_archive = 0.0
    if os.path.isdir(_DATA):
        for name in os.listdir(_DATA):
            if name.endswith(".tar.bz2"):
                try:
                    have_archive += os.path.getsize(os.path.join(_DATA, name)) / 1e9
                except OSError:
                    pass
    download_gb = max(0.0, ARCHIVE_GB - have_archive)
    if download_gb < 0.3:
        # ARCHIVE_GB is nominal; a fully-downloaded archive (~7.4 real vs
        # 7.5 nominal) must read as "nothing left", not "0.1 GB left".
        download_gb = 0.0
    tables_gb = 0.0 if stats().get("loaded") else TABLES_GB
    required_gb = download_gb + tables_gb + MARGIN_GB
    return {
        "download_gb": round(download_gb, 1),
        "required_gb": round(required_gb, 1),
        "free_gb": round(free_gb, 1),
        "can_fit": free_gb >= required_gb,
    }


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    ap = argparse.ArgumentParser(description="Download + load the MusicBrainz dump subset")
    ap.add_argument("--force", action="store_true", help="re-download even if current")
    ap.add_argument("--load-only", action="store_true", help="stream-load the existing archive")
    args = ap.parse_args()
    cb = lambda u: logger.info("progress %s", u)
    if args.load_only:
        for a in _archives():
            stream_load(a, cb)
    else:
        print(download_and_load(cb, force=args.force))


if __name__ == "__main__":
    main()


def delete_dump() -> Dict:
    """Remove the local dump: TRUNCATE the mb_* tables, drop the VERSION
    marker and the downloaded archives. The reverse of the wizard's opt-in —
    what makes a pre-ticked default fair.

    Sliced facts imported from peers live in the same tables and go with
    them; mb_slice_fetches is cleared too, so those names re-open for
    fetching instead of staying closed against data that is gone. Held
    under the loader's advisory lock — never mid-load."""
    import shutil

    from db_pool import get_conn
    freed = stats().get("size_bytes", 0)
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT pg_try_advisory_lock(%s)", (MB_LOAD_LOCK_KEY,))
            if not cur.fetchone()[0]:
                raise RuntimeError("a dump load is running")
            try:
                cur.execute("TRUNCATE " + ", ".join(t for t, _ in TABLES))
                cur.execute("DELETE FROM mb_slice_fetches")
                cur.execute("DELETE FROM mb_slice_blobs")
                cur.execute("DELETE FROM user_settings "
                            "WHERE key = 'musicbrainz.db_version'")
            finally:
                cur.execute("SELECT pg_advisory_unlock(%s)", (MB_LOAD_LOCK_KEY,))
    for path in (VERSION_PATH,):
        try:
            os.remove(path)
        except FileNotFoundError:
            pass
    for name in os.listdir(_DATA) if os.path.isdir(_DATA) else []:
        if name.endswith(".tar.bz2"):
            try:
                os.remove(os.path.join(_DATA, name))
            except OSError:
                pass
    logger.info("MB dump deleted (~%.1f GB of tables freed)", freed / 1e9)
    return {"deleted": True, "freed_bytes": freed}
