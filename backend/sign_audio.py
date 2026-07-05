#!/usr/bin/env python3
"""Sign a node's audio-analysis records and Worker-timestamp the batch.

Phase 1 of enrichment signing (docs/design/P2P-SYNC-INTEGRITY.md). For every
SIGNABLE track — owned-official (signing_whitelist) or a streamed clean source
(provenance.origin='stream') — this author-signs each unsigned CLAP segment and
audio_features row against the track's whole-track content-address (pcm_hash of
the decoded PCM), then batches all new signatures into one Merkle tree and has
the Worker timestamp the root. Authorship priority = the Worker date.

Incremental & idempotent: it only touches records whose signature IS NULL, so a
re-run signs whatever became signable since (e.g. streamed enrichments). Run it
on a daily cadence — one batch, one Worker timestamp per run (notary scaling).

    docker exec sautium-backend python /app/sign_audio.py [--limit N] [--dry-run]
"""

import argparse
import json
import logging
import os
import subprocess
import sys
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import numpy as np
import psycopg2
import psycopg2.extras

import record_sig as rs
from config import settings
from p2p_identity import load_signing_key

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("sign_audio")

WORKER_URL = "https://sautium-verify.sautium.workers.dev"

# Fixed order for the audio_features content hash — signer and verifier must
# agree byte-for-byte. Source_* columns describe provenance, not the analysis,
# and are excluded; analysis_version rides in the payload, not the blob.
FEATURE_ORDER = [
    "bpm", "key", "mode", "key_confidence", "energy", "energy_db", "brightness",
    "dynamic_range_db", "zero_crossing_rate", "danceability",
    "vocal_instrumental", "vocal_score", "instruments", "moods",
]


def _pubkey_hex(key) -> str:
    from cryptography.hazmat.primitives import serialization
    return key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw).hex()


def _parse_vector(text: str) -> np.ndarray:
    return np.array([float(x) for x in text.strip("[]").split(",")],
                    dtype=np.float32)


def _fmt(v) -> str:
    if v is None:
        return ""
    if isinstance(v, float):
        return repr(v)
    if isinstance(v, (dict, list)):
        return json.dumps(v, sort_keys=True, separators=(",", ":"))
    return str(v)


def canonical_features_blob(row: dict) -> bytes:
    return "|".join(_fmt(row[c]) for c in FEATURE_ORDER).encode("utf-8")


def _native_pcm_bytes(local_path: str) -> bytes:
    """Raw decoded PCM at the source's NATIVE rate & channels (f32le), before
    the -ac1 -ar48000 analysis conversion. Lossless decoding is deterministic
    across ffmpeg builds, so this is the stable content-address; the resample
    to the 48k-mono analysis frame (defined by grid_version) is a separate,
    tolerance-verified derivation. (Lossy sources still decode-vary — the
    chromaprint covers that.)"""
    out = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", local_path, "-f", "f32le", "pipe:1"],
        capture_output=True, check=True, timeout=300).stdout
    if not out:
        raise ValueError("ffmpeg produced no samples")
    return out


def _chromaprint(local_path: str):
    """AcoustID fingerprint of the recording (fpcalc, default 120s). The robust
    recording-identity anchor for cross-rip (step-2) verification — bound INTO
    the signature so it can never be added later without forfeiting the
    authorship-priority timestamp. Base64, ':'-free. None on failure (the
    record still signs against pcm_hash alone)."""
    try:
        out = subprocess.run(["fpcalc", "-plain", local_path],
                             capture_output=True, text=True, timeout=120, check=True)
        fp = out.stdout.strip()
        return fp or None
    except Exception as e:
        logger.warning("fpcalc failed for %s: %s", local_path, e)
        return None


def _timestamp_root(root: str) -> dict:
    req = urllib.request.Request(
        f"{WORKER_URL}/timestamp", method="POST",
        data=json.dumps({"root": root}).encode(),
        headers={"Content-Type": "application/json", "User-Agent": "Sautium/1.0"})
    return json.loads(urllib.request.urlopen(req, timeout=30).read())


def _signable_track_ids(cur) -> list:
    cur.execute("""
        SELECT DISTINCT mf.track_id::text AS tid
        FROM signing_whitelist sw
        JOIN album_variants av ON av.album_id = sw.album_id
        JOIN media_files mf    ON mf.album_variant_id = av.id
        UNION
        SELECT track_id::text FROM track_analysis_provenance WHERE origin = 'stream'
    """)
    return [r["tid"] for r in cur.fetchall()]


def _provenance(cur, track_id: str):
    """Return (pcm_hash, chromaprint) for a track, computing+storing it on the
    first call. None when the analysis-source file can't be decoded."""
    cur.execute("SELECT pcm_hash, chromaprint FROM track_analysis_provenance "
                "WHERE track_id = %s", (track_id,))
    row = cur.fetchone()
    if row:
        return row["pcm_hash"], row["chromaprint"]

    cur.execute("SELECT file_path FROM media_files "
                "WHERE track_id = %s AND is_analysis_source LIMIT 1", (track_id,))
    src = cur.fetchone()
    if not src:
        return None
    local_path = settings.translate_to_local_path(src["file_path"])
    try:
        pcm_bytes = _native_pcm_bytes(local_path)
    except Exception as e:
        logger.warning("decode failed for %s: %s", track_id[:8], e)
        return None

    ph = rs.pcm_hash(pcm_bytes)
    chromaprint = _chromaprint(local_path)
    cur.execute("""INSERT INTO track_analysis_provenance
                     (track_id, pcm_hash, chromaprint, origin)
                   VALUES (%s, %s, %s, 'local')
                   ON CONFLICT (track_id) DO NOTHING""", (track_id, ph, chromaprint))
    return ph, chromaprint


def run(limit=None, dry_run=False):
    key = load_signing_key(settings)
    if key is None:
        sys.exit("no signing identity — set P2P_USERNAME / P2P_PASSWORD")
    author = _pubkey_hex(key)

    conn = psycopg2.connect(settings.database_url)
    conn.cursor_factory = psycopg2.extras.RealDictCursor
    cur = conn.cursor()

    tracks = _signable_track_ids(cur)
    if limit:
        tracks = tracks[:limit]
    logger.info("signable tracks: %d", len(tracks))

    # pending: (table, pk, signature) collected across all tracks -> one batch
    pending, tracks_touched = [], 0
    for tid in tracks:
        prov = _provenance(cur, tid)
        if prov is None:
            continue
        pcm_hash, chromaprint = prov
        touched = False

        cur.execute("""SELECT id, model_id::text AS model, segment_index,
                              vector::text AS vec
                       FROM embedding_segments
                       WHERE track_id = %s AND signature IS NULL
                       ORDER BY model_id, segment_index""", (tid,))
        for seg in cur.fetchall():
            vh = rs.vector_hash(_parse_vector(seg["vec"]).tobytes())
            payload = rs.segment_payload(author, tid, pcm_hash, chromaprint,
                                         seg["model"], seg["segment_index"], vh)
            pending.append(("embedding_segments", seg["id"], rs.sign(payload, key)))
            touched = True

        cur.execute(f"""SELECT id, analysis_version,
                               {', '.join(FEATURE_ORDER)}
                        FROM audio_features
                        WHERE track_id = %s AND signature IS NULL""", (tid,))
        feat = cur.fetchone()
        if feat:
            fh = rs.blake2b_hex(canonical_features_blob(feat))
            payload = rs.features_payload(author, tid, pcm_hash, chromaprint,
                                          feat["analysis_version"], fh)
            pending.append(("audio_features", feat["id"], rs.sign(payload, key)))
            touched = True

        tracks_touched += touched

    if not pending:
        logger.info("nothing to sign")
        conn.commit()  # persist any provenance rows computed this run
        return

    leaves = [rs.record_leaf(sig) for _, _, sig in pending]
    root, proofs = rs.merkle_tree(leaves)
    logger.info("batch: %d records over %d tracks, root %s",
                len(pending), tracks_touched, root[:16])

    if dry_run:
        conn.rollback()
        logger.info("dry-run — not timestamped or stored")
        return

    ts = _timestamp_root(root)
    if not rs.verify_timestamp(root, ts["date"], ts["sig"], ts["authority"]):
        conn.rollback()
        sys.exit("Worker timestamp failed verification — aborting")

    cur.execute("""INSERT INTO signing_batches
                     (batch_root, author_pubkey, worker_date, worker_sig, authority)
                   VALUES (%s, %s, %s, %s, %s)""",
                (root, author, ts["date"], ts["sig"], ts["authority"]))
    for (table, pk, sig), proof in zip(pending, proofs):
        cur.execute(f"""UPDATE {table}
                        SET author_pubkey=%s, signature=%s, batch_root=%s,
                            merkle_proof=%s
                        WHERE id=%s""",
                    (author, sig, root, json.dumps(proof), pk))
    conn.commit()
    logger.info("signed %d records in batch %s @ %s",
                len(pending), root[:16], ts["date"])


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int)
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    run(limit=a.limit, dry_run=a.dry_run)
