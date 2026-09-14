"""Share export / import — Product B of docs/design/BACKUP.md.

"A dump another collector can merge" already exists as a wire format: the
sync payload. This module writes it to a file and reads it back through the
same gate a P2P pull or a carry push goes through (desktop.sync_client), so
the file invents no format and gets no second importer. The seed bundle
(backend/seed_export.py, seed_import.py) is one instance of the same
builders; it stays a caller.

The file — `sautium-export-<node>-<date>.jsonl.gz` — is gzip'd JSON LINES,
not one JSON document: an "everything I own" export of a 40k-track library
is ~1.7 GB of segment bundles, and a single document would have to be held
whole in memory on both ends. Lines, in order:

    {"format": "sautium-export", "version": 1, "identity_rule": N,
     "exporter": {"pubkey", "username", "created_at", "app"}, "scope": {…}}
    {"section": "<structural table>", "rows": [...]}     FK order, batches first
    {"envelope": "<category>", "group": "enrichment"|"analysis", "data": {…}}
    {"summary": {"albums", "tracks", "artists", "analysed_tracks", "items"}}
    {"end": true, "sha256": "<every byte above>", "signature": "<hex>"}

The signature is the exporter's node key over the running digest, so a
recipient knows who packed the file and that nothing was cut or edited; the
records inside carry their own seals, which the gate verifies one by one.
Import is two passes on purpose: the first hashes the whole file and checks
the trailer, the second applies — a streamed import can take nothing back,
so nothing is applied from a file that has not passed whole.

Content: the structural rows the records need (like the seed: albums,
tracklists, credits, genres, descriptions), then FIRST-HAND sealed records
per sync category — what this node observed itself, never a re-export of
what it pulled from others (sync_queries' first_hand filter: sign_audio's
_SIGNABLE_SRC for analysis, the row's `imported` flag for enrichment).
Scope: the carry gates — albums this node owns a file of, or is engaged
with (a completed listen), or an explicit artist / album list; never the
whole phantom layer.
"""

import gzip
import hashlib
import json
import logging
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterator, List, Optional

import psycopg2

from desktop.p2p import sync_queries as sq
from uuid_utils import IDENTITY_RULE

logger = logging.getLogger(__name__)

FORMAT = "sautium-export"
VERSION = 1
FILE_SUFFIX = ".jsonl.gz"
SIGN_PREFIX = b"sautium-export:v1:"
ROWS_PER_LINE = 500
SCOPES = ("engaged", "owned", "artists", "albums")

ProgressFn = Callable[..., None]


class ShareError(Exception):
    """Anything that stops an export or an import; the message is for the user."""


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _json_line(obj) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                      default=str) + "\n"


# ---------------------------------------------------------------------------
# The file: writer and reader (DB-free)
# ---------------------------------------------------------------------------

class ExportWriter:
    """Lines into an open text stream; every byte before the trailer goes into
    the digest the trailer signs."""

    def __init__(self, fp, header: dict, sign: Callable[[bytes], bytes]):
        self._fp = fp
        self._sign = sign
        self._hash = hashlib.sha256()
        self.lines = 0
        self._write({**header, "format": FORMAT, "version": VERSION,
                     "identity_rule": IDENTITY_RULE})

    def _write(self, obj) -> None:
        line = _json_line(obj)
        self._hash.update(line.encode("utf-8"))
        self._fp.write(line)
        self.lines += 1

    def section(self, name: str, rows) -> None:
        if isinstance(rows, dict):                      # the batches map
            self._write({"section": name, "rows": rows})
            return
        for i in range(0, len(rows), ROWS_PER_LINE):
            self._write({"section": name, "rows": rows[i:i + ROWS_PER_LINE]})
        if not rows:
            self._write({"section": name, "rows": []})

    def envelope(self, group: str, category: str, data: dict) -> None:
        self._write({"envelope": category, "group": group, "data": data})

    def finish(self, summary: dict) -> dict:
        self._write({"summary": summary})
        digest = self._hash.digest()
        trailer = {"end": True, "sha256": digest.hex(),
                   "signature": self._sign(SIGN_PREFIX + digest).hex()}
        self._fp.write(_json_line(trailer))
        self.lines += 1
        return trailer


def _verify_signature(pubkey_hex: str, digest: bytes, signature_hex: str) -> bool:
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    try:
        Ed25519PublicKey.from_public_bytes(bytes.fromhex(pubkey_hex)).verify(
            bytes.fromhex(signature_hex), SIGN_PREFIX + digest)
        return True
    except (InvalidSignature, ValueError):
        return False


def read_export(path: Path) -> Iterator[dict]:
    """Yield the header, every body line, then the summary; verify the
    trailer against the running digest and the header's key before the
    summary is handed out. Raises ShareError on any defect — a file that
    stops before its trailer is truncated, one whose bytes do not match the
    signed digest was edited."""
    digest = hashlib.sha256()
    header = None
    ended = False
    with gzip.open(path, "rt", encoding="utf-8", newline="\n") as fp:
        for raw in fp:
            if ended:
                raise ShareError("data after the trailer")
            try:
                obj = json.loads(raw)
            except ValueError:
                raise ShareError("not a Sautium export (a line is not JSON)")
            if header is None:
                if obj.get("format") != FORMAT:
                    raise ShareError("not a Sautium export file")
                if obj.get("version") != VERSION:
                    raise ShareError(f"export format v{obj.get('version')} — this Sautium "
                                     f"reads v{VERSION}; update first")
                if obj.get("identity_rule") != IDENTITY_RULE:
                    raise ShareError(f"the file was written under identity rule "
                                     f"v{obj.get('identity_rule')}, this node runs v{IDENTITY_RULE} "
                                     "— both sides must run the same Sautium")
                exporter = obj.get("exporter") or {}
                if not isinstance(exporter.get("pubkey"), str):
                    raise ShareError("the header names no exporter key")
                header = obj
            if obj.get("end") is True:
                if obj.get("sha256") != digest.hexdigest():
                    raise ShareError("the file was edited or damaged: its bytes do not "
                                     "match the digest the trailer signs")
                if not _verify_signature(header["exporter"]["pubkey"], digest.digest(),
                                         str(obj.get("signature", ""))):
                    raise ShareError("the trailer signature does not verify against the "
                                     "exporter's key")
                ended = True
                continue
            digest.update(raw.encode("utf-8"))
            yield obj
    if header is None:
        raise ShareError("empty file")
    if not ended:
        raise ShareError("truncated: the file ends before its signed trailer")


def verify_export(path: Path, *, progress: Optional[ProgressFn] = None) -> dict:
    """Pass one: the whole file through read_export. Returns {"header",
    "summary", "size"}; the summary is what the import will do."""
    progress = progress or (lambda *_a, **_k: None)
    header = summary = None
    for i, obj in enumerate(read_export(path)):
        if i == 0:
            header = obj
        elif "summary" in obj:
            summary = obj["summary"]
        if i % 50 == 0:
            progress("verifying", lines=i)
    if summary is None:
        raise ShareError("the file has no summary line")
    return {"header": header, "summary": summary, "size": path.stat().st_size}


# ---------------------------------------------------------------------------
# Scope
# ---------------------------------------------------------------------------

_OWNED_ALBUMS = """
    SELECT DISTINCT av.album_id::text AS id
      FROM album_variants av JOIN media_files mf ON mf.album_variant_id = av.id"""

_ENGAGED_ALBUMS = """
    SELECT al.id::text AS id FROM albums al
     WHERE EXISTS (SELECT 1 FROM album_variants av
                     JOIN media_files mf ON mf.album_variant_id = av.id
                    WHERE av.album_id = al.id)
        OR EXISTS (SELECT 1 FROM album_tracks at2
                     JOIN listening_history lh ON lh.track_id = at2.track_id
                    WHERE at2.album_id = al.id AND lh.completed AND NOT lh.skipped)"""


def resolve_artists(conn, names: List[str]) -> tuple[list[dict], list[str]]:
    """Names (or uuids) → artists rows; the names nothing matched come back
    so the caller can say so instead of silently exporting less."""
    found, missing = [], []
    for name in names:
        name = name.strip()
        if not name:
            continue
        rows = sq.db_query(conn, """
            SELECT id::text AS id, name FROM artists
             WHERE id::text = %(n)s OR lower(name) = lower(%(n)s)
                OR id IN (SELECT artist_id FROM artist_name_aliases
                           WHERE lower(alias_latin) = lower(%(n)s))
             ORDER BY (id::text = %(n)s) DESC, name LIMIT 5""", {"n": name})
        if rows:
            found.extend(rows)
        else:
            missing.append(name)
    return found, missing


def scope_albums(conn, kind: str, *, artists: Optional[List[str]] = None,
                 albums: Optional[List[str]] = None) -> tuple[list[str], dict]:
    """Album ids for a scope + the description the header carries."""
    if kind == "owned":
        rows = sq.db_query(conn, _OWNED_ALBUMS + " ORDER BY 1")
        return [r["id"] for r in rows], {"kind": "owned"}
    if kind == "engaged":
        rows = sq.db_query(conn, _ENGAGED_ALBUMS + " ORDER BY 1")
        return [r["id"] for r in rows], {"kind": "engaged"}
    if kind == "artists":
        found, missing = resolve_artists(conn, artists or [])
        if missing:
            raise ShareError("no such artist here: " + ", ".join(missing))
        if not found:
            raise ShareError("name at least one artist")
        ids = [a["id"] for a in found]
        rows = sq.db_query(conn, """
            SELECT DISTINCT album_id::text AS id FROM (
                SELECT album_id FROM album_artists WHERE artist_id = ANY(%(a)s::uuid[])
                UNION
                SELECT at2.album_id FROM album_tracks at2
                  JOIN track_artists ta ON ta.track_id = at2.track_id
                 WHERE ta.artist_id = ANY(%(a)s::uuid[])
            ) u ORDER BY 1""", {"a": ids})
        return [r["id"] for r in rows], {"kind": "artists",
                                          "artists": sorted({a["name"] for a in found})}
    if kind == "albums":
        ids = [a.strip() for a in (albums or []) if a.strip()]
        if not ids:
            raise ShareError("name at least one album id")
        rows = sq.db_query(conn, "SELECT id::text AS id FROM albums WHERE id = ANY(%s::uuid[]) ORDER BY 1",
                           [ids])
        missing = sorted(set(ids) - {r["id"] for r in rows})
        if missing:
            raise ShareError("no such album here: " + ", ".join(missing))
        return [r["id"] for r in rows], {"kind": "albums", "albums": len(rows)}
    raise ShareError(f"unknown scope {kind!r} (one of {', '.join(SCOPES)})")


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------

def export_filename(pubkey: str, when: Optional[datetime] = None) -> str:
    when = when or datetime.now(timezone.utc)
    return f"sautium-export-{pubkey[:12]}-{when.strftime('%Y-%m-%d')}{FILE_SUFFIX}"


def _unique_path(directory: Path, name: str) -> Path:
    path = directory / name
    if not path.exists():
        return path
    stem = name[:-len(FILE_SUFFIX)]
    return directory / f"{stem}-{datetime.now(timezone.utc).strftime('%H%M%S')}{FILE_SUFFIX}"


def export_file(conn, out_dir: Path, *, kind: str, exporter: dict,
                sign: Callable[[bytes], bytes], artists: Optional[List[str]] = None,
                albums: Optional[List[str]] = None, app: Optional[dict] = None,
                progress: Optional[ProgressFn] = None,
                cancel: Optional[threading.Event] = None) -> dict:
    """Write `<out_dir>/sautium-export-<node>-<date>.jsonl.gz` for the scope.
    Returns {"path", "size", "summary"}."""
    import seed_export

    progress = progress or (lambda *_a, **_k: None)
    cancel = cancel or threading.Event()

    def check_cancel() -> None:
        if cancel.is_set():
            raise ShareError("export cancelled")

    progress("scope")
    album_ids, scope = scope_albums(conn, kind, artists=artists, albums=albums)
    if not album_ids:
        raise ShareError("nothing to export: the scope holds no albums")
    track_ids, artist_ids = seed_export.collect_ids(conn, album_ids)
    progress("scope", albums=len(album_ids), tracks=len(track_ids), artists=len(artist_ids))
    check_cancel()

    out_dir.mkdir(parents=True, exist_ok=True)
    final_path = _unique_path(out_dir, export_filename(exporter["pubkey"]))
    part_path = final_path.with_name(final_path.name + ".part")
    items: dict = {}
    analysed: set = set()
    header = {"exporter": {**exporter, "created_at": _now_iso(), "app": app or {}},
              "scope": {**scope, "albums": len(album_ids), "tracks": len(track_ids),
                        "artists": len(artist_ids)}}
    try:
        with gzip.open(part_path, "wt", encoding="utf-8", newline="\n") as fp:
            writer = ExportWriter(fp, header, sign)
            for section, rows in seed_export.structural_sections(conn, album_ids, track_ids, artist_ids):
                check_cancel()
                writer.section(section, rows)
            done_tracks = 0
            for group, category, env in seed_export.envelope_chunks(
                    conn, track_ids, artist_ids, first_hand=True):
                check_cancel()
                if env["items"]:
                    writer.envelope(group, category, env)
                items[category] = items.get(category, 0) + len(env["items"])
                if category == "segments":
                    analysed.update(i["track_uuid"] for i in env["items"])
                    done_tracks = min(done_tracks + sq.SEGMENTS_MAX_UUIDS, len(track_ids))
                    progress("writing", tracks_done=done_tracks, tracks=len(track_ids))
            summary = {"albums": len(album_ids), "tracks": len(track_ids),
                       "artists": len(artist_ids), "analysed_tracks": len(analysed),
                       "items": items}
            writer.finish(summary)
        part_path.replace(final_path)
    except BaseException:
        part_path.unlink(missing_ok=True)
        raise
    size = final_path.stat().st_size
    logger.info("share export written: %s (%d bytes, %s)", final_path, size, summary)
    return {"path": final_path, "size": size, "summary": summary}


# ---------------------------------------------------------------------------
# Import
# ---------------------------------------------------------------------------

def carry_budget(conn) -> int:
    """`sync.carry_limit` — foreign tracks this node is willing to hold — or
    the shipped default when the row was never written."""
    rows = sq.db_query(conn, "SELECT value FROM user_settings WHERE key = 'sync.carry_limit'")
    if rows and rows[0]["value"] is not None:
        try:
            return int(rows[0]["value"])
        except (TypeError, ValueError):
            pass
    return sq.CARRY_DEFAULT_BUDGET


def plan_import(path: Path, db_dsn: str, *, progress: Optional[ProgressFn] = None) -> dict:
    """Pass one plus what the node thinks of it: the header and summary, the
    carry budget, and whether the file needs an explicit yes (more analysed
    tracks than the budget, or the streaming library switched off)."""
    import seed_import

    info = verify_export(path, progress=progress)
    conn = psycopg2.connect(db_dsn)
    try:
        budget = carry_budget(conn)
        layer_off = seed_import.phantom_layer_off(conn)
    finally:
        conn.close()
    analysed = int(info["summary"].get("analysed_tracks") or 0)
    info.update(budget=budget, over_budget=analysed > budget, phantom_layer_off=layer_off,
                needs_confirm=analysed > budget)
    return info


def apply_import(path: Path, db_dsn: str, *, confirmed: bool = False,
                 progress: Optional[ProgressFn] = None,
                 cancel: Optional[threading.Event] = None) -> dict:
    """Verify (pass one), then apply (pass two): structural sections through
    seed_import.insert_structural, envelopes through the sync client's
    verify-and-import gate, then the post-import classifiers on the artists
    the file named. Returns the plan plus per-category imported counts."""
    import seed_import
    from desktop.sync_client import SyncClient

    progress = progress or (lambda *_a, **_k: None)
    cancel = cancel or threading.Event()
    plan = plan_import(path, db_dsn, progress=progress)
    if plan["phantom_layer_off"]:
        raise ShareError("the streaming library (discovery.phantom_layer) is switched off "
                         "— albums you do not own could not be added; switch it on to import")
    if plan["needs_confirm"] and not confirmed:
        raise ShareError(f"{plan['summary']['analysed_tracks']:,} analysed tracks exceed this "
                         f"node's carry budget of {plan['budget']:,} — confirm to import anyway")

    total_tracks = int(plan["summary"].get("tracks") or 0)
    imported: dict = {}
    artist_ids: list = []
    conn = psycopg2.connect(db_dsn)
    client = SyncClient(api_client=None, db_dsn=db_dsn)
    try:
        done = 0
        for obj in read_export(path):
            if cancel.is_set():
                raise ShareError("import cancelled")
            if "section" in obj:
                if obj["section"] == "artists":
                    artist_ids.extend(r["id"] for r in obj["rows"])
                seed_import.insert_structural(conn, {obj["section"]: obj["rows"]})
            elif "envelope" in obj:
                category = obj["envelope"]
                n = client._import_items(category, obj["data"])
                imported[category] = imported.get(category, 0) + n
                if category == "segments":
                    done = min(done + sq.SEGMENTS_MAX_UUIDS, total_tracks)
                    progress("importing", tracks_done=done, tracks=total_tracks)
        if artist_ids:
            progress("classifying")
            client._update_artist_gender(artist_ids)
            client._update_artist_is_vocalist(artist_ids)
    finally:
        client._close_conn()
        conn.close()
    plan["imported"] = imported
    logger.info("share import applied from %s: %s", path.name, imported)
    return plan
