"""
The HQPlayer library as a source of album variants.

HQPlayer — Desktop or Embedded — scans its own library and answers
<LibraryGet/> with every directory and file it holds, tags included. A copy
of an album that lives there (a disk on an HQPlayer Embedded box, a share it
mounts, a hi-res subset copied to it) is one more VARIANT of the album, the
same thing a CD rip next to a vinyl rip already is, located at that HQPlayer
(album_variants.location = 'hqplayer'); its files are hqp_library_files,
media_files' mirror without bytes. The entries go through the scanner's own
import (scanner.import_metadata), so an album a local scan already minted
gains a variant instead of a twin and keeps its analysis, chromaprint and
listening history — the same UUID v5 identity from the same tags. HQPlayer
reads FLAC tags as our scanner does (99.7 % identical on the reference
library, 2026-09-27); mp3 tags it reads worse (ID3v1 truncation, encodings,
folder names standing in), a known limit.

A sync is explicit (Settings › Library, `python -m hqp_library`) or
event-driven: the HQPlayer output re-checks <LibraryGetHash/> when it
attaches or reconnects. A sync never removes rows; forgetting files the
library no longer lists is the explicit, confirmed rescan
(`forget_missing`), which keeps the scanner's guard against an empty answer.

Protocol facts (verified 2026-09-27 against Desktop 6 and Embedded 6, engine
6.2.3): the whole library comes back as ONE line of XML (39 618 files,
7.4 MB, 1.2 s), declared utf-8 but carrying raw bytes from tags;
LibraryFile.hash is an MD5 of the file name and LibraryDirectory.hash of
the directory path — change detection only; about 11 % of files have no
`number` (the file name's leading digits stand in); disc numbers exist only
as folder names; the per-file `artist` appears only when it differs from
the directory's (compilations, classical).
"""

import argparse
import logging
import os
import re
import socket
import threading
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

from sqlalchemy import text

from canon.identity import ORPHAN_TRACK_SQL
from database import get_db_context
from db_pool import db_execute, db_query
from scanner import AUDIO_EXTENSIONS, FileSink, import_metadata
from uuid_utils import is_lossless

logger = logging.getLogger(__name__)

# user_settings key per endpoint: the library hash the last COMPLETE sync saw.
HASH_SETTING = "hqp_library.hash:{host}:{port}"

_CONNECT_TIMEOUT = 2.0
_HASH_TIMEOUT = 10.0
# The whole library in one answer. 39k files took 1.2 s from Desktop; an
# Embedded box scanning a share may answer slower.
_LIBRARY_TIMEOUT = 300.0
# ET refuses the C0 control characters a tag can carry; the raw bytes of
# other encodings are already U+FFFD after the lenient decode.
_XML_CONTROL = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f]")
_DISC_FOLDER = re.compile(r"^(?:cd|disc|disk)\s*[-_ .]?\s*0*(\d{1,2})(?!\d)", re.I)
_LEADING_DIGITS = re.compile(r"^\s*(\d{1,3})(?!\d)")

Entry = Tuple[str, Dict[str, Any]]


def _command(host: str, port: int, xml: str, timeout: float) -> bytes:
    """One control-port command, one newline-framed line of XML back."""
    with socket.create_connection((host, port), timeout=_CONNECT_TIMEOUT) as s:
        s.settimeout(timeout)
        s.sendall(xml.encode("utf-8"))
        buf = bytearray()
        while not buf.endswith(b"\n"):
            chunk = s.recv(1 << 20)
            if not chunk:
                raise ConnectionError(
                    f"HQPlayer at {host}:{port} closed the connection mid-answer")
            buf += chunk
    return bytes(buf)


def library_hash(host: str, port: int) -> str:
    line = _command(host, port, "<LibraryGetHash/>", _HASH_TIMEOUT)
    m = re.search(rb'value="([^"]*)"', line)
    if not m:
        raise ConnectionError(
            f"HQPlayer at {host}:{port} gave no library hash: {line[:120]!r}")
    return m.group(1).decode("ascii")


def fetch_library(host: str, port: int) -> str:
    """The whole library as HQPlayer's XML text, decoded leniently."""
    return _command(host, port, '<LibraryGet picture="0"/>',
                    _LIBRARY_TIMEOUT).decode("utf-8", "replace")


def _int(value: Optional[str]) -> Optional[int]:
    try:
        return int(value) if value not in (None, "") else None
    except ValueError:
        return None


def _float(value: Optional[str]) -> Optional[float]:
    try:
        return float(value) if value not in (None, "") else None
    except ValueError:
        return None


def _year(date: Optional[str]) -> Optional[int]:
    return int(date[:4]) if date and date[:4].isdigit() else None


def _disc_from_folder(dir_path: str) -> Optional[int]:
    m = _DISC_FOLDER.match(os.path.basename(dir_path))
    return int(m.group(1)) if m else None


def _leading_digits(name: str) -> Optional[int]:
    m = _LEADING_DIGITS.match(name)
    return int(m.group(1)) if m else None


def parse_library(xml_text: str) -> Tuple[List[Entry], Dict[str, int]]:
    """LibraryGet → (dir_path, metadata) entries in the shape
    scanner.extract_metadata returns, ready for scanner.import_metadata.
    Paths are stored with forward slashes (the form HQPlayer's own file://
    URIs take on every platform). Returns the entries and the counts of
    files seen and skipped as unsupported."""
    counts = {"files": 0, "unsupported": 0}
    root = ET.fromstring(_XML_CONTROL.sub(" ", xml_text))
    entries: List[Entry] = []
    for d in root.iter("LibraryDirectory"):
        dir_path = (d.get("path") or "").replace("\\", "/").rstrip("/")
        if not dir_path:
            continue
        disc = _disc_from_folder(dir_path)
        dir_artist = d.get("artist") or None
        dir_date = d.get("date") or None
        dir_genre = d.get("genre") or None
        specs = {
            "sample_rate": _int(d.get("rate")),
            "bit_depth": _int(d.get("bits")),
            "bitrate": _int(d.get("bitrate")),
            "channels": _int(d.get("channels")),
        }
        for f in d.findall("LibraryFile"):
            counts["files"] += 1
            name = f.get("name") or ""
            stem, ext = os.path.splitext(name)
            ext = ext.lower()
            if ext not in AUDIO_EXTENSIONS:
                counts["unsupported"] += 1
                continue
            date = f.get("date") or dir_date
            metadata = {
                "file_path": f"{dir_path}/{name}",
                "title": f.get("song") or stem,
                "artist": f.get("artist") or dir_artist,
                "album_artist": dir_artist,
                "album": d.get("album") or None,
                "date": date,
                "release_year": _year(date),
                "genre": f.get("genre") or dir_genre,
                "track_number": _int(f.get("number")) or _leading_digits(name),
                "disc_number": disc or 1,
                "duration_seconds": _float(f.get("length")),
                "file_format": ext[1:].upper(),
                "is_lossless": is_lossless(ext),
                "hqp_file_hash": f.get("hash"),
                "hqp_dir_hash": d.get("hash"),
                **specs,
            }
            entries.append((dir_path, metadata))
    return entries, counts


def _known_paths(host: str, port: int) -> Dict[str, int]:
    """hqp_path → row id for everything this endpoint's variants hold."""
    rows = db_query("""
        SELECT f.id, f.hqp_path
        FROM hqp_library_files f
        JOIN album_variants av ON av.id = f.album_variant_id
        WHERE av.hqp_endpoint_host = %(h)s AND av.hqp_endpoint_port = %(p)s
    """, {"h": host, "p": port})
    return {r["hqp_path"]: r["id"] for r in rows}


def sync(host: str, port: int, *, force: bool = False,
         progress_cb: Optional[Callable] = None,
         cancel_check: Optional[Callable[[], bool]] = None) -> Dict[str, Any]:
    """Bring this endpoint's library into the catalogue: files already known
    are touched (last_seen_at), new ones imported; nothing is removed. A
    library whose hash matches the last complete sync is skipped unless
    `force`. A cancelled run leaves the old hash, so the next sync resumes
    (known paths cost one query). Returns per-step counts."""
    from routers.settings import _read, _write
    stats: Dict[str, Any] = {"unchanged": False, "library_files": 0, "unsupported": 0,
                             "known": 0, "added": 0, "errors": 0, "unique_tracks": 0,
                             "cancelled": False}
    key = HASH_SETTING.format(host=host, port=port)
    current = library_hash(host, port)
    if not force and current == _read(key):
        stats["unchanged"] = True
        return stats
    if progress_cb:
        progress_cb("Reading the HQPlayer library...", stats)
    entries, counts = parse_library(fetch_library(host, port))
    stats["library_files"], stats["unsupported"] = counts["files"], counts["unsupported"]
    known = _known_paths(host, port)
    seen_ids = [known[md["file_path"]] for _, md in entries if md["file_path"] in known]
    new = [e for e in entries if e[1]["file_path"] not in known]
    stats["known"] = len(seen_ids)
    if seen_ids:
        db_execute("UPDATE hqp_library_files SET last_seen_at = CURRENT_TIMESTAMP "
                   "WHERE id = ANY(%(ids)s)", {"ids": seen_ids})
    if new:
        import_metadata(new, sink=FileSink("hqplayer", host, port), stats=stats,
                        progress_cb=progress_cb, cancel_check=cancel_check)
        # A copy that just arrived is new in the collection: the Home feed
        # sorts variants by file_modified_at, which media_files' triggers keep
        # for local rips and nothing keeps for a copy — the moment the node
        # first saw it stands in.
        db_execute("""
            UPDATE album_variants av
               SET file_modified_at = (SELECT MIN(f.first_seen_at) FROM hqp_library_files f
                                       WHERE f.album_variant_id = av.id)
             WHERE av.location = 'hqplayer' AND av.file_modified_at IS NULL
               AND av.hqp_endpoint_host = %(h)s AND av.hqp_endpoint_port = %(p)s
        """, {"h": host, "p": port})
    if cancel_check and cancel_check():
        stats["cancelled"] = True
        return stats
    _write(key, current)
    logger.info("HQPlayer library %s:%s synced: %s", host, port, stats)
    return stats


def _forget(db, gone: List[int], since: Optional[datetime] = None) -> Dict[str, int]:
    """Remove hqp_library_files rows ``gone`` and what they alone justified:
    the endpoint's variants left without files, albums left with neither a
    variant nor a phantom tracklist, tracks nothing refers to
    (ORPHAN_TRACK_SQL — a track with analysis or a listen stays, as it does
    for a vanished local rip) and, when ``since`` is given, artists minted
    after it that no credit names any more (a cancelled import's raw
    credits; an artist that existed before keeps its bio and similars)."""
    stats = {"forgotten": 0, "orphan_variants": 0, "orphan_albums": 0,
             "orphan_tracks": 0, "orphan_artists": 0}
    rows = db.execute(text("""
        DELETE FROM hqp_library_files WHERE id = ANY(CAST(:ids AS int[]))
        RETURNING track_id, album_variant_id
    """), {"ids": gone}).fetchall()
    stats["forgotten"] = len(rows)
    track_ids = sorted({str(r[0]) for r in rows})
    variant_ids = sorted({r[1] for r in rows})
    if not track_ids:
        return stats
    credited = [str(r[0]) for r in db.execute(text("""
        SELECT DISTINCT artist_id FROM track_artists WHERE track_id = ANY(CAST(:tids AS uuid[]))
    """), {"tids": track_ids}).fetchall()]
    album_ids = [str(r[0]) for r in db.execute(text("""
        DELETE FROM album_variants
        WHERE id = ANY(CAST(:vids AS int[])) AND location = 'hqplayer'
          AND NOT EXISTS (SELECT 1 FROM hqp_library_files f
                          WHERE f.album_variant_id = album_variants.id)
        RETURNING album_id
    """), {"vids": variant_ids}).fetchall()]
    stats["orphan_variants"] = len(album_ids)
    if album_ids:
        credited += [str(r[0]) for r in db.execute(text("""
            SELECT DISTINCT artist_id FROM album_artists WHERE album_id = ANY(CAST(:aids AS uuid[]))
        """), {"aids": album_ids}).fetchall()]
        stats["orphan_albums"] = db.execute(text("""
            DELETE FROM albums
            WHERE id = ANY(CAST(:aids AS uuid[]))
              AND NOT EXISTS (SELECT 1 FROM album_variants av WHERE av.album_id = albums.id)
              AND NOT EXISTS (SELECT 1 FROM album_tracks at WHERE at.album_id = albums.id)
        """), {"aids": album_ids}).rowcount
    stats["orphan_tracks"] = db.execute(text(f"""
        DELETE FROM tracks t
        WHERE t.id = ANY(CAST(:tids AS uuid[])) AND {ORPHAN_TRACK_SQL.format(t='t')}
    """), {"tids": track_ids}).rowcount
    if since is not None and credited:
        stats["orphan_artists"] = db.execute(text("""
            DELETE FROM artists a
            WHERE a.id = ANY(CAST(:aids AS uuid[])) AND a.created_at >= :since
              AND NOT EXISTS (SELECT 1 FROM track_artists ta WHERE ta.artist_id = a.id)
              AND NOT EXISTS (SELECT 1 FROM album_artists aa WHERE aa.artist_id = a.id)
        """), {"aids": sorted(set(credited)), "since": since}).rowcount
    return stats


def forget_missing(host: str, port: int) -> Dict[str, Any]:
    """The explicit rescan: rows the library no longer lists go (see
    _forget). An empty answer is refused the way the scanner refuses an
    empty tree: a library being rebuilt on the HQPlayer side lists nothing,
    and every row would read as gone."""
    stats: Dict[str, Any] = {"refused": False, "checked": 0}
    entries, _counts = parse_library(fetch_library(host, port))
    listed = {md["file_path"] for _, md in entries}
    if not listed:
        logger.error("HQPlayer library %s:%s answered with no files — nothing forgotten", host, port)
        stats["refused"] = True
        return stats
    known = _known_paths(host, port)
    stats["checked"] = len(known)
    gone = [row_id for path, row_id in known.items() if path not in listed]
    if gone:
        with get_db_context() as db:
            stats.update(_forget(db, gone))
            db.commit()
    logger.info("HQPlayer library %s:%s rescan: %s", host, port, stats)
    return stats


def forget_endpoint(host: str, port: int, since: Optional[datetime] = None) -> Dict[str, Any]:
    """Everything this endpoint's library put into the catalogue goes (see
    _forget) — the HQPlayer was retired, or an import ran against the wrong
    one. ``since`` also drops the artists that import minted."""
    known = _known_paths(host, port)
    stats: Dict[str, Any] = {"checked": len(known)}
    if known:
        with get_db_context() as db:
            stats.update(_forget(db, list(known.values()), since))
            db.commit()
    logger.info("HQPlayer library %s:%s forgotten: %s", host, port, stats)
    return stats


# -- the background job (Settings, the HQPlayer output's attach) ----------------

_job_state: Dict[str, Any] = {
    "running": False,
    "cancel_requested": False,
    "progress": "",
    "stats": None,        # live stats dict while a sync runs
    "result": None,       # final result when done
    "endpoint": None,     # "host:port" of the run
    "mode": None,         # "sync" | "rescan"
}
_job_lock = threading.Lock()


def job_state() -> Dict[str, Any]:
    return dict(_job_state)


def start_job(host: str, port: int, *, force: bool = False, rescan: bool = False) -> bool:
    """Run a sync — or the explicit rescan — in a background thread.
    False when a job is already running."""
    with _job_lock:
        if _job_state["running"]:
            return False
        _job_state.update(running=True, cancel_requested=False, progress="Starting...",
                          stats=None, result=None, endpoint=f"{host}:{port}",
                          mode="rescan" if rescan else "sync")
    threading.Thread(target=_job, args=(host, port, force, rescan), daemon=True,
                     name="hqp-library-sync").start()
    return True


def cancel_job() -> bool:
    if not _job_state["running"]:
        return False
    _job_state["cancel_requested"] = True
    return True


def request_sync(host: str, port: int) -> None:
    """Event-driven entry for the HQPlayer output: on attach and on every
    return of a restarted HQPlayer, the library's hash is compared with the
    last complete sync and a sync runs only when it moved. Only an endpoint
    the owner has synced once is followed — the first import of a library
    is always the explicit button, never a side effect of choosing an
    output (an HQPlayer Desktop on this very library would otherwise import
    its 39k files behind the owner's back). Off the caller's thread: the
    status poller must not wait on the control port."""
    def _check() -> None:
        from routers.settings import _read
        last = _read(HASH_SETTING.format(host=host, port=port))
        if last is None:
            return
        try:
            if library_hash(host, port) != last:
                start_job(host, port)
        except OSError as e:
            logger.warning("HQPlayer library check at %s:%s skipped: %s", host, port, e)
    threading.Thread(target=_check, daemon=True, name="hqp-library-check").start()


def _job(host: str, port: int, force: bool, rescan: bool) -> None:
    from routers.settings import notify_library_subscribers
    state = _job_state
    started = datetime.now(timezone.utc)
    try:
        def progress_cb(msg: str, stats: Dict[str, Any]) -> None:
            state["progress"] = msg
            state["stats"] = dict(stats)
            notify_library_subscribers()

        if rescan:
            result = forget_missing(host, port)
            state["progress"] = ("Nothing forgotten: the library answered with no files"
                                 if result["refused"] else
                                 f"Rescan complete: {result['forgotten']} files forgotten")
        else:
            result = sync(host, port, force=force, progress_cb=progress_cb,
                          cancel_check=lambda: state["cancel_requested"])
            if result["unchanged"]:
                state["progress"] = "HQPlayer library unchanged"
            elif result["cancelled"]:
                state["progress"] = "Sync cancelled"
            else:
                if result["added"]:
                    from canon import post_import
                    post_import.run(state, result, started)
                    result["covers"] = caa.fill_held_album_covers()
                state["progress"] = (f"Sync complete: {result['added']} added, "
                                     f"{result['known']} already here")
        state["result"] = result
    except Exception as e:
        logger.error("HQPlayer library job failed: %s", e, exc_info=True)
        state["progress"] = f"Sync failed: {str(e)[:200]}"
        state["result"] = {"error": str(e)}
    finally:
        state["running"] = False
        try:
            notify_library_subscribers()
        except Exception:
            pass


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Sync an HQPlayer's library into this node's catalogue")
    ap.add_argument("--host", required=True)
    ap.add_argument("--port", type=int, default=4321)
    ap.add_argument("--force", action="store_true", help="ignore the stored library hash")
    ap.add_argument("--dry-run", action="store_true",
                    help="read the library and report what would be new; write nothing")
    ap.add_argument("--forget-missing", action="store_true",
                    help="the explicit rescan: remove rows the library no longer lists")
    ap.add_argument("--forget-endpoint", action="store_true",
                    help="remove everything this endpoint's library put into the catalogue")
    ap.add_argument("--since", help="with --forget-endpoint: also drop artists minted after this ISO time")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if args.dry_run:
        found, found_counts = parse_library(fetch_library(args.host, args.port))
        have = _known_paths(args.host, args.port)
        fresh = sum(1 for _, md in found if md["file_path"] not in have)
        print(f"library: {found_counts['files']} files ({found_counts['unsupported']} unsupported); "
              f"known here: {len(have)}; new: {fresh}")
    elif args.forget_missing:
        print(forget_missing(args.host, args.port))
    elif args.forget_endpoint:
        since = datetime.fromisoformat(args.since.replace("Z", "+00:00")) if args.since else None
        print(forget_endpoint(args.host, args.port, since))
    else:
        print(sync(args.host, args.port, force=args.force))
