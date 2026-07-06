#!/usr/bin/env python3
"""Sign a node's audio-analysis records and Worker-timestamp the batch.

Phase 1 of enrichment signing (docs/design/P2P-SYNC-INTEGRITY.md). A record is
signable when its LINKED analysis_sources row (registered at analysis time by
the scanner / stream enricher — never recomputed here) is signable material:

  - origin='local'  AND the track's album is in signing_whitelist
    (owned-official purchases; grey rips stay unsigned — privacy), or
  - origin='deezer' AND is_lossless — tier 3: a streamed clean source signs
    against the STREAM's pcm_hash, claiming no possession of any local rip.
    YouTube / lossy tiers never sign (decode-varying pcm_hash, lossy master).

Each unsigned CLAP segment (via its embeddings row) and audio_features row is
author-signed against that source's content-address; all new signatures batch
into one Merkle tree and the Worker timestamps the root. Authorship priority =
the Worker date. Records not yet linked to a source are skipped, not hashed
lazily — the link is the statement of WHAT was analyzed, and only the analysis
pass itself can make it.

Incremental & idempotent: only records whose signature IS NULL are touched, so
a re-run signs whatever became signable since. Run it on a daily cadence — one
batch, one Worker timestamp per run (notary scaling).

    docker exec sautium-backend python /app/sign_audio.py [--limit N] [--dry-run]
"""

import argparse
import json
import logging
import os
import sys
import urllib.request
from datetime import timezone

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
# agree byte-for-byte. analysis_version rides in the payload, not the blob.
FEATURE_ORDER = [
    "bpm", "key", "mode", "key_confidence", "energy", "energy_db", "brightness",
    "dynamic_range_db", "zero_crossing_rate", "danceability",
    "vocal_instrumental", "vocal_score", "instruments", "moods",
]

# A record signs only when its linked source is signable-classed AND
# first-hand (imported sources arrived over sync — signing analysis this node
# never computed would be authorship theft in reverse); 'local' additionally
# requires the track to be whitelisted (bound as %(wl)s).
_SIGNABLE_SRC = """(NOT src.imported
                    AND ((src.origin = 'deezer' AND src.is_lossless)
                         OR (src.origin = 'local' AND %(wl)s)))"""


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


def _timestamp_root(root: str) -> dict:
    req = urllib.request.Request(
        f"{WORKER_URL}/timestamp", method="POST",
        data=json.dumps({"root": root}).encode(),
        headers={"Content-Type": "application/json", "User-Agent": "Sautium/1.0"})
    return json.loads(urllib.request.urlopen(req, timeout=30).read())


def _signable_tracks(cur) -> dict:
    """{track_id: is_whitelisted} over both signable classes. A whitelisted
    track whose current analysis is still stream-linked signs the stream
    source now and re-signs after the owned re-analysis replaces it."""
    cur.execute("""
        SELECT DISTINCT mf.track_id::text AS tid
        FROM signing_whitelist sw
        JOIN album_variants av ON av.album_id = sw.album_id
        JOIN media_files mf    ON mf.album_variant_id = av.id
    """)
    whitelisted = {r["tid"] for r in cur.fetchall()}
    cur.execute("""
        SELECT DISTINCT track_id::text AS tid
        FROM analysis_sources
        WHERE origin = 'deezer' AND is_lossless AND NOT imported
    """)
    deezer = {r["tid"] for r in cur.fetchall()}
    return {tid: (tid in whitelisted) for tid in whitelisted | deezer}


def run(limit=None, dry_run=False):
    key = load_signing_key(settings)
    if key is None:
        sys.exit("no signing identity — set P2P_USERNAME / P2P_PASSWORD")
    author = _pubkey_hex(key)

    conn = psycopg2.connect(settings.database_url)
    conn.cursor_factory = psycopg2.extras.RealDictCursor
    cur = conn.cursor()

    tracks = _signable_tracks(cur)
    track_ids = sorted(tracks)
    if limit:
        track_ids = track_ids[:limit]
    logger.info("signable tracks: %d", len(track_ids))

    # pending: (table, pk, signature) collected across all tracks -> one batch
    pending, tracks_touched = [], 0
    for tid in track_ids:
        params = {"tid": tid, "wl": tracks[tid]}
        touched = False

        cur.execute(f"""
            SELECT s.id, s.segment_index, s.vector::text AS vec,
                   e.model_id::text AS model,
                   src.pcm_hash, src.chromaprint, src.duration_seconds,
                   src.grid_version
            FROM embedding_segments s
            JOIN embeddings e         ON e.id = s.embedding_id
            JOIN analysis_sources src ON src.id = e.analysis_source_id
            WHERE e.track_id = %(tid)s AND s.signature IS NULL
              AND {_SIGNABLE_SRC}
            ORDER BY s.segment_index""", params)
        for seg in cur.fetchall():
            vh = rs.vector_hash(_parse_vector(seg["vec"]).tobytes())
            payload = rs.segment_payload(
                author, tid, seg["pcm_hash"], seg["chromaprint"],
                seg["duration_seconds"],
                seg["model"], seg["segment_index"], vh,
                grid_version=seg["grid_version"])
            pending.append(("embedding_segments", seg["id"], rs.sign(payload, key)))
            touched = True

        cur.execute(f"""
            SELECT a.id, a.analysis_version, {', '.join(FEATURE_ORDER)},
                   src.pcm_hash, src.chromaprint, src.duration_seconds
            FROM audio_features a
            JOIN analysis_sources src ON src.id = a.analysis_source_id
            WHERE a.track_id = %(tid)s AND a.signature IS NULL
              AND {_SIGNABLE_SRC}""", params)
        feat = cur.fetchone()
        if feat:
            fh = rs.blake2b_hex(canonical_features_blob(feat))
            payload = rs.features_payload(author, tid, feat["pcm_hash"],
                                          feat["chromaprint"],
                                          feat["duration_seconds"],
                                          feat["analysis_version"], fh)
            pending.append(("audio_features", feat["id"], rs.sign(payload, key)))
            touched = True

        tracks_touched += touched

    if not pending:
        logger.info("nothing to sign")
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


def verify_all() -> bool:
    """Re-verify the seal of every signed record against the stored content.

    For each signed segment / audio_features row it rebuilds the exact signed
    payload FROM the stored data — the content hashes are recomputed from the
    actual vector / feature values, so this also proves the stored data is what
    was sealed — then runs the full chain (author signature → Merkle inclusion
    → Worker timestamp by a trusted authority). A signed row whose provenance
    link is missing counts as INVALID: the seal asserts a content-address the
    row no longer carries. Reports valid/invalid counts; returns True iff every
    seal holds. (This is the seal check; a --deep audit that also re-decodes
    the audio to confirm pcm_hash against the file is a separate, heavier pass.)"""
    from birth_authority import TRUSTED_AUTHORITIES

    conn = psycopg2.connect(settings.database_url)
    conn.cursor_factory = psycopg2.extras.RealDictCursor
    cur = conn.cursor()

    cur.execute("SELECT batch_root, worker_date, worker_sig, authority "
                "FROM signing_batches")
    batches = {
        b["batch_root"]: (
            b["worker_date"].astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            b["worker_sig"], b["authority"])
        for b in cur.fetchall()
    }

    ok, bad, bad_samples = 0, 0, []

    def _seal_ok(payload, r):
        b = batches.get(r["batch_root"])
        return bool(b and rs.verify_seal(
            payload, r["signature"], r["author_pubkey"], r["merkle_proof"],
            r["batch_root"], b[0], b[1], b[2], TRUSTED_AUTHORITIES))

    cur.execute("""SELECT s.author_pubkey, s.signature, s.merkle_proof, s.batch_root,
                          e.track_id::text tid, e.model_id::text model,
                          s.segment_index idx, s.vector::text vec,
                          p.pcm_hash, p.chromaprint, p.duration_seconds,
                          p.grid_version
                   FROM embedding_segments s
                   JOIN embeddings e ON e.id = s.embedding_id
                   LEFT JOIN analysis_sources p ON p.id = e.analysis_source_id
                   WHERE s.signature IS NOT NULL""")
    for r in cur.fetchall():
        if r["pcm_hash"] is None:
            bad += 1
            if len(bad_samples) < 5:
                bad_samples.append(f"segment {r['tid'][:8]}#{r['idx']} UNLINKED")
            continue
        vh = rs.vector_hash(_parse_vector(r["vec"]).tobytes())
        payload = rs.segment_payload(r["author_pubkey"], r["tid"], r["pcm_hash"],
                                     r["chromaprint"], r["duration_seconds"],
                                     r["model"], r["idx"], vh,
                                     grid_version=r["grid_version"])
        if _seal_ok(payload, r):
            ok += 1
        else:
            bad += 1
            if len(bad_samples) < 5:
                bad_samples.append(f"segment {r['tid'][:8]}#{r['idx']}")

    cur.execute(f"""SELECT a.author_pubkey, a.signature, a.merkle_proof, a.batch_root,
                           a.track_id::text tid, a.analysis_version,
                           {', '.join('a.' + c for c in FEATURE_ORDER)},
                           p.pcm_hash, p.chromaprint, p.duration_seconds
                    FROM audio_features a
                    LEFT JOIN analysis_sources p ON p.id = a.analysis_source_id
                    WHERE a.signature IS NOT NULL""")
    for r in cur.fetchall():
        if r["pcm_hash"] is None:
            bad += 1
            if len(bad_samples) < 5:
                bad_samples.append(f"features {r['tid'][:8]} UNLINKED")
            continue
        fh = rs.blake2b_hex(canonical_features_blob(r))
        payload = rs.features_payload(r["author_pubkey"], r["tid"], r["pcm_hash"],
                                      r["chromaprint"], r["duration_seconds"],
                                      r["analysis_version"], fh)
        if _seal_ok(payload, r):
            ok += 1
        else:
            bad += 1
            if len(bad_samples) < 5:
                bad_samples.append(f"features {r['tid'][:8]}")

    logger.info("seal verification: %d valid, %d invalid", ok, bad)
    if bad_samples:
        logger.warning("invalid: %s", bad_samples)
    return bad == 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--verify", action="store_true",
                    help="re-verify the seal of every signed record; sign nothing")
    a = ap.parse_args()
    if a.verify:
        sys.exit(0 if verify_all() else 1)
    run(limit=a.limit, dry_run=a.dry_run)
