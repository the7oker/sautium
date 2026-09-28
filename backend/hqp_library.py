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
import time
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


def get_info(host: str, port: int) -> Dict[str, str]:
    """What HQPlayer says about itself: name (the machine's on Desktop, a
    generic "HQPlayerEmbedded" on a box) and product."""
    line = _command(host, port, "<GetInfo/>", _HASH_TIMEOUT).decode("utf-8", "replace")
    out = {}
    for k in ("name", "product"):
        m = re.search(rf'\b{k}="([^"]*)"', line)
        out[k] = m.group(1) if m else ""
    return out


_DISCOVER_TIMEOUT = 1.5


def parse_discover(reply: bytes) -> Optional[Dict[str, str]]:
    """The answer to HQPlayer's own discovery datagram —
    `<discover name="VH11" result="NA" version="Signalyst HQPlayer Desktop 6"/>`
    — as name + product (the version string names the product, major
    included, so `is_embedded` reads it like GetInfo's). None for anything
    else on that port."""
    tag = re.search(r"<discover\b([^>]*)", reply.decode("utf-8", "replace"))
    if tag is None:
        return None
    out = {}
    for k in ("name", "version"):
        m = re.search(rf'\b{k}="([^"]*)"', tag.group(1))   # the XML prolog has a version too
        out[k] = m.group(1) if m else ""
    return {"name": out["name"], "product": out["version"]}


def discover(host: str, port: int = 4321, timeout: float = _DISCOVER_TIMEOUT) -> Optional[Dict[str, str]]:
    """Is there an HQPlayer at this address? Its control port answers a
    `<discover/>` datagram (UDP, the same port number) with its name and
    product — what HQPlayer Client broadcasts for, verified 2026-09-28
    against Desktop 6 and Embedded 6. Sent unicast, so it crosses the
    docker bridge like the DLNA sweep's M-SEARCH does, and the reply comes
    back from 4321, which the bridge's port-restricted NAT lets through.
    None when nothing HQPlayer-shaped answered in time."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.settimeout(timeout)
    try:
        s.sendto(b"<discover/>\n", (host, port))
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                data, addr = s.recvfrom(4096)
            except socket.timeout:
                return None
            facts = parse_discover(data)
            if facts is not None:
                return facts
        return None
    except OSError:
        return None
    finally:
        s.close()


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


_ENDPOINT_COLS = "id, name, host, port, product, hqp_name, library_hash, last_synced_at"


_GENERIC_NAMES = ("", "hqplayer", "hqplayerembedded", "hqplayerdesktop")


def label(name: Optional[str], product: Optional[str]) -> str:
    """What the UI calls an HQPlayer, everywhere it names one: the product —
    Desktop, Embedded — and the name that tells two apart (a Desktop's is
    its machine's), unless that name is the generic one a box gives itself
    ("HQPlayerEmbedded" says nothing the product does not)."""
    m = re.search(r"(desktop|embedded)", product or "", re.I)
    kind = f"HQPlayer {m.group(1).capitalize()}" if m else "HQPlayer"
    if re.sub(r"[^a-z0-9]", "", (name or "").lower()) in _GENERIC_NAMES:
        return kind
    return f"{kind} · {name}"


def address_key(host: str, port: int) -> Tuple[str, int]:
    """One HQPlayer per box: this machine's aliases — localhost, the Docker
    host, its LAN address (auth_hmac.is_own_address) — name the same
    HQPlayer, so they share a key; anywhere else the address is the key."""
    from auth_hmac import is_own_address
    return ("here" if is_own_address(host) else host.strip().lower(), int(port))


def endpoint_by_address(host: str, port: int) -> Optional[Dict[str, Any]]:
    """The row at this address — the alias of it another caller registered
    included: a Desktop the picker met at host.docker.internal is the one
    the scan sees at the LAN address."""
    rows = db_query(f"SELECT {_ENDPOINT_COLS} FROM hqp_endpoints WHERE host = %(h)s AND port = %(p)s",
                    {"h": host, "p": port})
    if rows:
        return dict(rows[0])
    key = address_key(host, port)
    if key[0] != "here":
        return None
    for r in db_query(f"SELECT {_ENDPOINT_COLS} FROM hqp_endpoints "
                      "WHERE host IS NOT NULL AND port = %(p)s ORDER BY id", {"p": port}):
        if address_key(r["host"], r["port"]) == key:
            return dict(r)
    return None


def endpoint_id_for(host: Optional[str], port: Optional[int]) -> Optional[int]:
    """The id of the endpoint answering at this address, None when its
    library was never imported (no row) — the ranking callers' key."""
    if not host:
        return None
    ep = endpoint_by_address(host, port)
    return ep["id"] if ep else None


def resolve_endpoint(host: str, port: int) -> Tuple[Optional[Dict[str, Any]], Optional[str], Dict[str, Any]]:
    """Which library answers at this address: the row at the address, if the
    HQPlayer there is still the one the row met (same name and product —
    another box that inherited the address is another library); else the
    row whose last complete sync saw this very library hash (the same
    HQPlayer at a new address); else nothing yet. Returns (row | None, how:
    'address' | 'library' | None, facts) — facts = what the HQPlayer
    reported plus the hash, for ensure_endpoint to store. Reads only."""
    info = get_info(host, port)
    facts = {"hqp_name": info["name"], "product": info["product"],
             "current_hash": library_hash(host, port)}
    ep = endpoint_by_address(host, port)
    if ep and (ep["hqp_name"] or info["name"]) == info["name"] \
          and (ep["product"] or info["product"]) == info["product"]:
        return ep, "address", facts
    rows = db_query(f"SELECT {_ENDPOINT_COLS} FROM hqp_endpoints "
                    "WHERE library_hash = %(h)s AND library_hash IS NOT NULL ORDER BY id",
                    {"h": facts["current_hash"]})
    if rows:
        return dict(rows[0]), "library", facts
    return None, None, facts


def is_embedded(product: Optional[str]) -> bool:
    return "embedded" in (product or "").lower()


def has_own_library(host: str) -> bool:
    """Only an HQPlayer on ANOTHER machine has a library of its own — an
    Embedded box with a disk, a Desktop on a second computer. One on this
    machine, whatever the product, reads the node's own music folder: its
    library IS this catalogue, and importing it brought every album in as
    a copy once (2026-09-27), so it is never synced. Read off the address
    (auth_hmac.is_own_address), as the way files reach it is."""
    from auth_hmac import is_own_address
    return not is_own_address(host)


def ensure_endpoint(host: str, port: int, resolved=None) -> Dict[str, Any]:
    """The endpoint row for the library at this address — moved here when
    the same library answers from a new address, minted when it is new
    (named after what HQPlayer calls itself), the address freed from a row
    another HQPlayer left behind. Stores what the HQPlayer reported.
    `resolved` is a resolve_endpoint answer the caller already holds."""
    ep, how, facts = resolved or resolve_endpoint(host, port)
    if ep is None or how == "library":
        # the address is this library's now: whoever held it before has moved on
        db_execute("UPDATE hqp_endpoints SET host = NULL, port = NULL "
                   "WHERE host = %(h)s AND port = %(p)s AND id IS DISTINCT FROM %(id)s",
                   {"h": host, "p": port, "id": ep["id"] if ep else None})
    if ep is None:
        row = db_execute("""
            INSERT INTO hqp_endpoints (name, host, port, product, hqp_name)
            VALUES (%(name)s, %(h)s, %(p)s, %(product)s, %(hqp_name)s)
            RETURNING id
        """, {"name": facts["hqp_name"] or host, "h": host, "p": port, "product": facts["product"],
              "hqp_name": facts["hqp_name"]})
        logger.info("HQPlayer library at %s:%s is new here: endpoint %s %r", host, port, row["id"],
                    facts["hqp_name"] or host)
    else:
        if how == "library":
            logger.info("HQPlayer library %r moved: %s:%s -> %s:%s", ep["name"], ep["host"], ep["port"], host, port)
        # a row minted from an address alone (migration 030) takes the name
        # HQPlayer gives itself the first time it is heard; a row met by an
        # alias of its own address keeps the address it was registered at
        if how == "address":
            host, port = ep["host"], int(ep["port"])
        db_execute("""
            UPDATE hqp_endpoints
               SET host = %(h)s, port = %(p)s, product = %(product)s, hqp_name = %(hqp_name)s,
                   name = CASE WHEN name = host OR name = %(h)s THEN COALESCE(NULLIF(%(hqp_name)s, ''), name)
                               ELSE name END
             WHERE id = %(id)s
        """, {"h": host, "p": port, "product": facts["product"], "hqp_name": facts["hqp_name"],
              "id": ep["id"]})
    ep = endpoint_by_address(host, port)
    ep["current_hash"] = facts["current_hash"]
    return ep


def register(host: str, port: int) -> Optional[Dict[str, Any]]:
    """The row for an HQPlayer the owner chose or added: every HQPlayer
    picked in the Output picker has one, a Desktop included — a LIBRARY
    only for an Embedded, the sync refuses the rest. This machine's aliases
    (localhost, the Docker host, its LAN address) are one HQPlayer: an own
    address finds the row another alias minted (endpoint_by_address) and
    leaves its address alone. None when the box does not answer control
    right now — the address is the owner's choice all the same; the
    output's attach registers it on the first contact."""
    try:
        return ensure_endpoint(host, port)
    except OSError as e:
        logger.info("HQPlayer at %s:%s is not answering — no endpoint row yet: %s", host, port, e)
        return None


def _known_paths(endpoint_id: int) -> Dict[str, int]:
    """hqp_path → row id for everything this endpoint's variants hold."""
    rows = db_query("""
        SELECT f.id, f.hqp_path
        FROM hqp_library_files f
        JOIN album_variants av ON av.id = f.album_variant_id
        WHERE av.hqp_endpoint_id = %(e)s
    """, {"e": endpoint_id})
    return {r["hqp_path"]: r["id"] for r in rows}


def sync(host: str, port: int, *, force: bool = False,
         progress_cb: Optional[Callable] = None,
         cancel_check: Optional[Callable[[], bool]] = None) -> Dict[str, Any]:
    """Bring this endpoint's library into the catalogue: files already known
    are touched (last_seen_at), new ones imported; nothing is removed. A
    library whose hash matches the last complete sync is skipped unless
    `force`. A cancelled run leaves the old hash, so the next sync resumes
    (known paths cost one query). Returns per-step counts."""
    stats: Dict[str, Any] = {"unchanged": False, "refused": None, "library_files": 0,
                             "unsupported": 0, "known": 0, "added": 0, "errors": 0,
                             "unique_tracks": 0, "cancelled": False}
    resolved = resolve_endpoint(host, port)
    if not has_own_library(host):
        logger.warning("HQPlayer at %s:%s runs on this machine and reads this node's own library; not synced",
                       host, port)
        # Its own row (the Output picker registers every HQPlayer the owner
        # chose) keeps its address; a library row that still names this
        # address met another HQPlayer here and has moved on.
        if resolved[1] != "address":
            db_execute("UPDATE hqp_endpoints SET host = NULL, port = NULL WHERE host = %(h)s AND port = %(p)s",
                       {"h": host, "p": port})
        stats["refused"] = "here"
        return stats
    ep = ensure_endpoint(host, port, resolved)
    current = ep["current_hash"]
    if not force and current == ep["library_hash"]:
        stats["unchanged"] = True
        return stats
    if progress_cb:
        progress_cb("Reading the HQPlayer library...", stats)
    entries, counts = parse_library(fetch_library(host, port))
    stats["library_files"], stats["unsupported"] = counts["files"], counts["unsupported"]
    known = _known_paths(ep["id"])
    seen_ids = [known[md["file_path"]] for _, md in entries if md["file_path"] in known]
    new = [e for e in entries if e[1]["file_path"] not in known]
    stats["known"] = len(seen_ids)
    if seen_ids:
        db_execute("UPDATE hqp_library_files SET last_seen_at = CURRENT_TIMESTAMP "
                   "WHERE id = ANY(%(ids)s)", {"ids": seen_ids})
        # A variant imported before the import stamped raw_title (2026-09-27)
        # takes the library's album tag now — the scan-time title the canon's
        # edition rules read.
        tagged = [(known[md["file_path"]], md["album"]) for _, md in entries
                  if md["file_path"] in known and md.get("album")]
        db_execute("""
            UPDATE album_variants av SET raw_title = x.title
              FROM (SELECT unnest(%(ids)s::int[]) AS id, unnest(%(titles)s::text[]) AS title) x
              JOIN hqp_library_files hf ON hf.id = x.id
             WHERE av.id = hf.album_variant_id AND av.raw_title IS NULL
        """, {"ids": [i for i, _ in tagged], "titles": [t for _, t in tagged]})
    if new:
        import_metadata(new, sink=FileSink("hqplayer", ep["id"]), stats=stats,
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
               AND av.hqp_endpoint_id = %(e)s
        """, {"e": ep["id"]})
    if cancel_check and cancel_check():
        stats["cancelled"] = True
        return stats
    db_execute("UPDATE hqp_endpoints SET library_hash = %(h)s, last_synced_at = CURRENT_TIMESTAMP "
               "WHERE id = %(e)s", {"h": current, "e": ep["id"]})
    logger.info("HQPlayer library %r (%s:%s) synced: %s", ep["name"], host, port, stats)
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
    resolved = resolve_endpoint(host, port)
    if not has_own_library(host):
        stats["refused"] = "here"
        return stats
    ep = ensure_endpoint(host, port, resolved)
    entries, _counts = parse_library(fetch_library(host, port))
    listed = {md["file_path"] for _, md in entries}
    if not listed:
        logger.error("HQPlayer library %s:%s answered with no files — nothing forgotten", host, port)
        stats["refused"] = True
        return stats
    known = _known_paths(ep["id"])
    stats["checked"] = len(known)
    gone = [row_id for path, row_id in known.items() if path not in listed]
    if gone:
        with get_db_context() as db:
            stats.update(_forget(db, gone))
            db.commit()
    logger.info("HQPlayer library %r rescan: %s", ep["name"], stats)
    return stats


def forget_endpoint(host: str, port: int, since: Optional[datetime] = None) -> Dict[str, Any]:
    """Everything this endpoint's library put into the catalogue goes (see
    _forget) — the HQPlayer was retired, or an import ran against the wrong
    one. ``since`` also drops the artists that import minted."""
    ep = endpoint_by_address(host, port)
    if ep is None:
        return {"checked": 0}
    return forget_endpoint_id(ep["id"], since)


def forget_endpoint_id(endpoint_id: int, since: Optional[datetime] = None) -> Dict[str, Any]:
    """forget_endpoint by row — an endpoint that no longer answers anywhere
    (a friend's streamer that went home) has no address to name it by. The
    row goes with its rows."""
    known = _known_paths(endpoint_id)
    stats: Dict[str, Any] = {"checked": len(known)}
    if known:
        with get_db_context() as db:
            stats.update(_forget(db, list(known.values()), since))
            db.commit()
    db_execute("DELETE FROM hqp_endpoints WHERE id = %(e)s", {"e": endpoint_id})
    logger.info("HQPlayer library endpoint %s forgotten: %s", endpoint_id, stats)
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
    return of a restarted HQPlayer, the endpoint is registered if it was
    chosen while off, then the library's hash is compared with the last
    complete sync and a sync runs only when it moved. Only an endpoint the
    owner has synced once is followed — the first import of a library is
    always the explicit button, never a side effect of choosing an output
    (an HQPlayer Desktop on this very library would otherwise import its
    39k files behind the owner's back). Off the caller's thread: the
    status poller must not wait on the control port."""
    def _check() -> None:
        # the first contact of an HQPlayer chosen while it was off registers it
        ep = endpoint_by_address(host, port) or register(host, port)
        if ep is None or ep["library_hash"] is None:
            return
        try:
            if library_hash(host, port) != ep["library_hash"]:
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
            if result["refused"]:
                state["progress"] = ("Not synced: an HQPlayer on this machine reads this node's "
                                     "own library — there is nothing to import")
            elif result["unchanged"]:
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
        ep, how, facts = resolve_endpoint(args.host, args.port)
        found, found_counts = parse_library(fetch_library(args.host, args.port))
        have = _known_paths(ep["id"]) if ep else {}
        fresh = sum(1 for _, md in found if md["file_path"] not in have)
        print(f"endpoint: {ep['name'] + ' (by ' + how + ')' if ep else 'new — ' + (facts['hqp_name'] or args.host)}; "
              f"library: {found_counts['files']} files ({found_counts['unsupported']} unsupported); "
              f"known here: {len(have)}; new: {fresh}")
    elif args.forget_missing:
        print(forget_missing(args.host, args.port))
    elif args.forget_endpoint:
        since = datetime.fromisoformat(args.since.replace("Z", "+00:00")) if args.since else None
        print(forget_endpoint(args.host, args.port, since))
    else:
        print(sync(args.host, args.port, force=args.force))
