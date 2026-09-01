"""Import the cold-start seed bundle (backend/seed/seed_v1.json.gz).

Runs once per node from db_migrate.apply_pending, keyed by the `seed_v1`
marker row — the marker is written only after a COMPLETE import, so a
partial landing (killed boot, transient DB error) retries on the next
start. Every statement is idempotent: structural rows land with
ON CONFLICT DO NOTHING (a node that already holds a row keeps its own —
the master and any owning node are no-ops by construction), and the
enrichment/analysis half replays the bundle's verbatim pull envelopes
through SyncClient._import_items — the same verify-and-import gate a P2P
pull uses, so seals are checked and first-hand rows are protected
identically.

A failure here never gates the service: the node without the seed is a
fully functional pre-seed node, so errors are logged loudly and retried
next boot instead of failing startup.
"""

import gzip
import json
import logging
from pathlib import Path

import psycopg2
import psycopg2.extras

from uuid_utils import IDENTITY_RULE

logger = logging.getLogger(__name__)

BUNDLE_PATH = Path(__file__).resolve().parent / "seed" / "seed_v1.json.gz"

_ENRICHMENT_CATEGORIES = ("artist_bios", "artist_tags", "similar_artists")
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


def _load_bundle() -> dict | None:
    if not BUNDLE_PATH.exists():
        return None
    with gzip.open(BUNDLE_PATH, "rt", encoding="utf-8") as fh:
        return json.load(fh)


def _phantom_layer_off(conn) -> bool:
    """True only when the owner EXPLICITLY switched the phantom layer off —
    a missing row is the default (on), per settings._DEFAULTS."""
    with conn.cursor() as cur:
        cur.execute("SELECT value FROM user_settings WHERE key = 'discovery.phantom_layer'")
        row = cur.fetchone()
    return row is not None and row[0] is False


def _insert_structural(conn, structural: dict) -> None:
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
    "artist_bios": ("SELECT COUNT(DISTINCT artist_id) FROM artist_bios WHERE artist_id = ANY(%s::uuid[])",
                    "artist_uuid"),
    "artist_tags": ("SELECT COUNT(DISTINCT artist_id) FROM artist_tags WHERE artist_id = ANY(%s::uuid[])",
                    "artist_uuid"),
    "similar_artists": ("SELECT COUNT(DISTINCT artist_id) FROM similar_artists WHERE artist_id = ANY(%s::uuid[])",
                        "artist_uuid"),
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


def apply_seed(conn, db_dsn: str) -> dict:
    """Import the bundle. Returns {"complete": bool, ...}; the caller writes
    the marker only on complete=True."""
    bundle = _load_bundle()
    if bundle is None:
        logger.info("seed: no bundle at %s — skipping", BUNDLE_PATH)
        return {"complete": False, "skipped": "no_bundle"}

    if bundle.get("identity_rule") != IDENTITY_RULE:
        logger.error(
            "seed: bundle identity rule v%s does not match this node's v%s — "
            "skipping; the fix is re-exporting the bundle on the current rule",
            bundle.get("identity_rule"), IDENTITY_RULE)
        return {"complete": False, "skipped": "identity_rule_mismatch"}

    if _phantom_layer_off(conn):
        logger.info("seed: discovery.phantom_layer is explicitly off — skipping")
        return {"complete": False, "skipped": "phantom_layer_off"}

    out: dict = {"complete": False}
    try:
        _insert_structural(conn, bundle["structural"])
    except psycopg2.Error as e:
        conn.rollback()
        logger.error("seed: structural import failed, will retry next start: %s", e)
        out["error"] = str(e)[:500]
        return out

    envelopes = {
        **{c: bundle["enrichment"][c] for c in _ENRICHMENT_CATEGORIES},
        **{c: bundle["analysis"][c] for c in _ANALYSIS_CATEGORIES},
    }

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
        artist_ids = [a["id"] for a in bundle["structural"]["artists"]]
        client._update_artist_gender(artist_ids)
        client._update_artist_is_vocalist(artist_ids)
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
