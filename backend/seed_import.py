"""Import the cold-start seed bundle.

The bundle is not in the tree: a 17 MB artifact regenerated whenever the
master re-signs, and every version once committed would sit in the history
of every clone for good. backend/seed/bundle.json (tracked, written by
seed_export.py) names the current version, its download URL — a GitHub
Release asset — and its sha256; the file itself lives under
settings.seed_dir and is fetched on the first start that needs it. A file
whose digest does not match is discarded, and the import waits for the
next start like any other transient failure here.

Runs once per node from db_migrate.apply_pending, keyed by the `seed_v{N}`
marker row (N from bundle.json) — the marker is written only after a
COMPLETE import, so a partial landing (killed boot, transient DB error, no
network yet) retries on the next start. Every statement is idempotent:
structural rows land with
ON CONFLICT DO NOTHING (a node that already holds a row keeps its own —
the master and any owning node are no-ops by construction), and the
analysis half replays the bundle's verbatim pull envelopes
through SyncClient._import_items — the same verify-and-import gate a P2P
pull uses, so seals are checked and first-hand rows are protected
identically.

A failure here never gates the service: the node without the seed is a
fully functional pre-seed node, so errors are logged loudly and retried
next boot instead of failing startup.
"""

import gzip
import hashlib
import json
import logging
import os
import tempfile
import urllib.request
from pathlib import Path

import psycopg2
import psycopg2.extras

from config import settings
from uuid_utils import IDENTITY_RULE

logger = logging.getLogger(__name__)

BUNDLE_INFO_PATH = Path(__file__).resolve().parent / "seed" / "bundle.json"
DOWNLOAD_TIMEOUT_S = 60

_ANALYSIS_CATEGORIES = ("segments", "audio_features", "track_mbids")

# (bundle section, INSERT statement, VALUES template, columns) in FK order.
# Sealed rows are inserted whole with imported=TRUE — the seal-guard
# triggers fire on UPDATE only, and honest provenance is the point.
_STRUCTURAL_INSERTS = [
    ("artists",
     "INSERT INTO artists (id, name, raw_name, name_latin, artist_type, gender, is_vocalist) VALUES %s ON CONFLICT DO NOTHING",
     "(%s::uuid, %s, %s, %s, %s::artist_type, %s::artist_gender, %s::artist_vocalist)",
     ("id", "name", "raw_name", "name_latin", "artist_type", "gender", "is_vocalist")),
    ("albums",
     """INSERT INTO albums (id, title, title_latin, release_year, label, catalog_number,
                            total_tracks, musicbrainz_id, mb_match_confidence, cover_url,
                            author_pubkey, signature, batch_root, merkle_proof, imported)
        VALUES %s ON CONFLICT DO NOTHING""",
     "(%s::uuid, %s, %s, %s, %s, %s, %s, %s::uuid, %s::mb_match_confidence, %s, %s, %s, %s, %s, TRUE)",
     ("id", "title", "title_latin", "release_year", "label", "catalog_number",
      "total_tracks", "musicbrainz_id", "mb_match_confidence", "cover_url",
      "author_pubkey", "signature", "batch_root", "merkle_proof")),
    ("tracks",
     "INSERT INTO tracks (id, title, title_latin) VALUES %s ON CONFLICT DO NOTHING",
     "(%s::uuid, %s, %s)",
     ("id", "title", "title_latin")),
    ("album_tracks",
     """INSERT INTO album_tracks (album_id, track_id, disc, position, recording_mbid,
                                  length_ms, author_pubkey, signature, batch_root,
                                  merkle_proof, imported)
        VALUES %s ON CONFLICT DO NOTHING""",
     "(%s::uuid, %s::uuid, %s, %s, %s::uuid, %s, %s, %s, %s, %s, TRUE)",
     ("album_id", "track_id", "disc", "position", "recording_mbid", "length_ms",
      "author_pubkey", "signature", "batch_root", "merkle_proof")),
    ("track_artists",
     "INSERT INTO track_artists (track_id, artist_id, role) VALUES %s ON CONFLICT DO NOTHING",
     "(%s::uuid, %s::uuid, %s::credit_role)",
     ("track_id", "artist_id", "role")),
    ("album_artists",
     "INSERT INTO album_artists (album_id, artist_id, role, mbid) VALUES %s ON CONFLICT DO NOTHING",
     "(%s::uuid, %s::uuid, %s::credit_role, %s::uuid)",
     ("album_id", "artist_id", "role", "mbid")),
    ("artist_mbids",
     "INSERT INTO artist_mbids (mbid, artist_id, confidence, name, about) VALUES %s ON CONFLICT DO NOTHING",
     "(%s::uuid, %s::uuid, %s::mb_match_confidence, %s, %s)",
     ("mbid", "artist_id", "confidence", "name", "about")),
    ("genres",
     "INSERT INTO genres (id, name) VALUES %s ON CONFLICT DO NOTHING",
     "(%s::uuid, %s)",
     ("id", "name")),
    ("album_genres",
     "INSERT INTO album_genres (album_id, genre_id, source, count) VALUES %s ON CONFLICT DO NOTHING",
     "(%s::uuid, %s::uuid, %s, %s)",
     ("album_id", "genre_id", "source", "count")),
    ("album_descriptions",
     "INSERT INTO album_descriptions (album_id, source, summary, content, url) VALUES %s ON CONFLICT DO NOTHING",
     "(%s::uuid, %s, %s, %s, %s)",
     ("album_id", "source", "summary", "content", "url")),
    ("seed_picks",
     "INSERT INTO seed_picks (album_id, tier, rank) VALUES %s ON CONFLICT DO NOTHING",
     "(%s::uuid, %s, %s)",
     ("album_id", "tier", "rank")),
]

_JSON_COLUMNS = {"merkle_proof"}


def bundle_info() -> dict | None:
    """The tracked description of the current bundle — version, url, sha256,
    size; None in a tree that ships no seed."""
    if not BUNDLE_INFO_PATH.exists():
        return None
    return json.loads(BUNDLE_INFO_PATH.read_text(encoding="utf-8"))


def bundle_path(info: dict) -> Path:
    return Path(settings.seed_dir) / f"seed_v{info['version']}.json.gz"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        while chunk := fh.read(1 << 20):
            digest.update(chunk)
    return digest.hexdigest()


def ensure_bundle(info: dict) -> Path | None:
    """The bundle file `info` describes, downloaded when absent; None when it
    cannot be had right now (no network, a cut transfer, a digest mismatch)
    — the caller skips and the next start tries again."""
    path = bundle_path(info)
    if path.exists():
        if _sha256(path) == info["sha256"]:
            return path
        logger.warning("seed: %s does not match bundle.json — fetching again", path.name)
        path.unlink()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = tempfile.NamedTemporaryFile(dir=path.parent, prefix=path.name + ".", delete=False)
    tmp.close()
    try:
        logger.info("seed: downloading %s (%.1f MB)", info["url"], info["size"] / 1_048_576)
        with urllib.request.urlopen(info["url"], timeout=DOWNLOAD_TIMEOUT_S) as resp, \
                open(tmp.name, "wb") as out:
            while chunk := resp.read(1 << 20):
                out.write(chunk)
        if _sha256(Path(tmp.name)) != info["sha256"]:
            logger.error("seed: the downloaded bundle does not match the digest in "
                         "bundle.json — discarded")
            return None
        os.replace(tmp.name, path)
        return path
    except OSError as e:          # urllib's URLError is one, as is a full disk
        logger.warning("seed: download failed, will retry next start: %s", e)
        return None
    finally:
        if os.path.exists(tmp.name):
            os.unlink(tmp.name)


def _load_bundle(info: dict) -> dict | None:
    path = ensure_bundle(info)
    if path is None:
        return None
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        return json.load(fh)


def phantom_layer_off(conn) -> bool:
    """True only when the owner EXPLICITLY switched the phantom layer off —
    a missing row is the default (on), per settings._DEFAULTS."""
    with conn.cursor() as cur:
        cur.execute("SELECT value FROM user_settings WHERE key = 'discovery.phantom_layer'")
        row = cur.fetchone()
    return row is not None and row[0] is False


def insert_structural(conn, structural: dict) -> None:
    """Structural rows in FK order, batches first; every statement is
    ON CONFLICT DO NOTHING (a node that holds a row keeps its own). Shared
    with the share import (backend/share.py), which feeds it one section
    chunk at a time."""
    from desktop.sync_client import SyncClient
    with conn.cursor() as cur:
        batches = structural.get("batches") or {}
        SyncClient._insert_batches(cur, batches, set(batches))
        for section, sql, template, columns in _STRUCTURAL_INSERTS:
            rows = [
                tuple(
                    psycopg2.extras.Json(item.get(col))
                    if col in _JSON_COLUMNS and item.get(col) is not None
                    else item.get(col)
                    for col in columns
                )
                for item in structural.get(section) or []
            ]
            if rows:
                psycopg2.extras.execute_values(cur, sql, rows, template=template,
                                               page_size=500)
    conn.commit()


def _count_present(conn, sql: str, ids: list[str]) -> int:
    if not ids:
        return 0
    with conn.cursor() as cur:
        cur.execute(sql, [ids])
        return int(cur.fetchone()[0])


# Presence-based completeness: "does the node now hold the layer", counted
# by entity — indifferent to whether the import inserted the row or the
# precedence rules kept a local/first-hand one (both are success).
_PRESENCE_SQL = {
    "segments": ("SELECT COUNT(DISTINCT track_id) FROM embeddings WHERE track_id = ANY(%s::uuid[])",
                 "track_uuid"),
    "audio_features": ("SELECT COUNT(DISTINCT track_id) FROM audio_features WHERE track_id = ANY(%s::uuid[])",
                       "track_uuid"),
    "track_mbids": ("SELECT COUNT(DISTINCT track_id) FROM track_mbids WHERE track_id = ANY(%s::uuid[])",
                    "track_uuid"),
}


def _check_completeness(conn, bundle: dict, envelopes: dict) -> dict:
    """Per-category {expected, present}; expected = distinct entities in the
    bundle envelope (or rows for the structural anchors)."""
    out = {}
    pick_ids = [p["album_id"] for p in bundle["structural"]["seed_picks"]]
    out["seed_picks"] = {
        "expected": len(pick_ids),
        "present": _count_present(
            conn, "SELECT COUNT(*) FROM seed_picks WHERE album_id = ANY(%s::uuid[])",
            pick_ids),
    }
    out["albums"] = {
        "expected": len(bundle["structural"]["albums"]),
        "present": _count_present(
            conn, "SELECT COUNT(*) FROM albums WHERE id = ANY(%s::uuid[])",
            [a["id"] for a in bundle["structural"]["albums"]]),
    }
    for category, envelope in envelopes.items():
        sql, key = _PRESENCE_SQL[category]
        entities = sorted({item[key] for item in envelope["items"]})
        out[category] = {
            "expected": len(entities),
            "present": _count_present(conn, sql, entities),
        }
    return out


def apply_seed(conn, db_dsn: str, info: dict) -> dict:
    """Import the bundle `info` describes. Returns {"complete": bool, ...};
    the caller writes the marker only on complete=True."""
    bundle = _load_bundle(info)
    if bundle is None:
        return {"complete": False, "skipped": "bundle_unavailable"}

    if bundle.get("identity_rule") != IDENTITY_RULE:
        logger.error(
            "seed: bundle identity rule v%s does not match this node's v%s — "
            "skipping; the fix is re-exporting the bundle on the current rule",
            bundle.get("identity_rule"), IDENTITY_RULE)
        return {"complete": False, "skipped": "identity_rule_mismatch"}

    if phantom_layer_off(conn):
        logger.info("seed: discovery.phantom_layer is explicitly off — skipping")
        return {"complete": False, "skipped": "phantom_layer_off"}

    out: dict = {"complete": False}
    try:
        insert_structural(conn, bundle["structural"])
    except psycopg2.Error as e:
        conn.rollback()
        logger.error("seed: structural import failed, will retry next start: %s", e)
        out["error"] = str(e)[:500]
        return out

    envelopes = {c: bundle["analysis"][c] for c in _ANALYSIS_CATEGORIES}

    from desktop.sync_client import SyncClient
    client = SyncClient(api_client=None, db_dsn=db_dsn)
    try:
        for category, envelope in envelopes.items():
            if envelope["items"]:
                # _import_items verifies every seal, honors first-hand
                # precedence and commits per category — a mid-run failure
                # leaves prior categories landed and this one retried next
                # boot.
                client._import_items(category, envelope)
    finally:
        client._close_conn()

    counts = _check_completeness(conn, bundle, envelopes)
    out["counts"] = {k: v["present"] for k, v in counts.items()}
    short = {k: v for k, v in counts.items() if v["present"] < v["expected"]}
    if short:
        logger.warning(
            "seed: incomplete, will retry next start — %s",
            ", ".join(f"{k} {v['present']}/{v['expected']}" for k, v in short.items()))
        return out

    out["complete"] = True
    logger.info("seed: imported %d picks (%d albums, %d analyzed tracks)",
                counts["seed_picks"]["present"], counts["albums"]["present"],
                counts.get("segments", {}).get("present", 0))
    return out
