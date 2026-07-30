#!/usr/bin/env python3
"""Sign a node's audio-analysis records and Worker-timestamp the batch.

Phase 1 of enrichment signing (docs/design/P2P-SYNC-INTEGRITY.md). A record is
signable when its LINKED analysis_sources row (registered at analysis time by
the scanner / stream enricher — never recomputed here) is signable material:

  - origin='local' — ALL first-hand local analysis signs (the per-album
    signing_whitelist gate was dropped 2026-07-07: an unsigned network breaks
    integrity testing and the sync verify chain; selective privacy policy is
    off), or
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

import psycopg2
import psycopg2.extras

import record_sig as rs
from config import settings
from p2p_identity import load_signing_key
from record_sig import FEATURE_ORDER, canonical_features_blob

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("sign_audio")

WORKER_URL = "https://sautium-verify.sautium.workers.dev"

# A record signs only when its linked source is signable-classed AND
# first-hand (imported sources arrived over sync — signing analysis this node
# never computed would be authorship theft in reverse).
_SIGNABLE_SRC = """(NOT src.imported
                    AND ((src.origin = 'deezer' AND src.is_lossless)
                         OR src.origin = 'local'))"""


def _pubkey_hex(key) -> str:
    from cryptography.hazmat.primitives import serialization
    return key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw).hex()


def _vector_bytes(text: str) -> bytes:
    """pgvector text → the canonical float32-LE bytes vector_hash covers."""
    return rs.vector_to_bytes([float(x) for x in text.strip("[]").split(",")])


def _timestamp_root(root: str) -> dict:
    req = urllib.request.Request(
        f"{WORKER_URL}/timestamp", method="POST",
        data=json.dumps({"root": root}).encode(),
        headers={"Content-Type": "application/json", "User-Agent": "Sautium/1.0"})
    return json.loads(urllib.request.urlopen(req, timeout=30).read())


def _signable_tracks(cur) -> list:
    """Track ids with at least one first-hand signable source."""
    cur.execute("""
        SELECT DISTINCT track_id::text AS tid
        FROM analysis_sources
        WHERE NOT imported
          AND (origin = 'local' OR (origin = 'deezer' AND is_lossless))
    """)
    return [r["tid"] for r in cur.fetchall()]


# Enrichment records join the SAME batch as the audio ones: one Merkle root,
# one Worker timestamp, one round trip. They are cheap to sign and there is no
# reason to pay for a second notarisation.
#
# Only NOT imported rows. A signature here says "this source told ME this" —
# signing a row a peer sent us would restate their observation as our own, and
# the network would lose the one thing these signatures are for.
_ENRICHMENT_SOURCES = {
    "artist_bio": ("""
        SELECT b.id, b.artist_id::text AS entity, b.source, b.fetched_at,
               b.summary, b.content, b.url, b.listeners, b.playcount
        FROM artist_bios b
        WHERE b.signature IS NULL AND NOT b.imported""", "artist_bios"),
    "artist_tag": ("""
        SELECT at2.id, at2.artist_id::text AS entity, at2.source, at2.fetched_at,
               t.name AS tag_name, at2.weight
        FROM artist_tags at2 JOIN tags t ON t.id = at2.tag_id
        WHERE at2.signature IS NULL AND NOT at2.imported""", "artist_tags"),
    "similar_artist": ("""
        SELECT sa.id, sa.artist_id::text AS entity, sa.source, sa.fetched_at,
               sa.similar_artist_id::text AS similar_artist_uuid,
               sa.match_score::float AS match_score
        FROM similar_artists sa
        WHERE sa.signature IS NULL AND NOT sa.imported""", "similar_artists"),
    "track_stat": ("""
        SELECT ts.id, ts.track_id::text AS entity, ts.source, ts.fetched_at,
               ts.listeners, ts.playcount
        FROM track_stats ts
        WHERE ts.signature IS NULL AND NOT ts.imported""", "track_stats"),
    "genre_description": ("""
        SELECT gd.id, gd.genre_id::text AS entity, gd.source, gd.fetched_at,
               gd.summary, gd.content, gd.url
        FROM genre_descriptions gd
        WHERE gd.signature IS NULL AND NOT gd.imported""", "genre_descriptions"),
}


def _collect_enrichment(cur, author, key, pending, chunk=20000) -> int:
    """Append signed enrichment records to `pending`. Returns how many."""
    added = 0
    for kind, (sql, table) in _ENRICHMENT_SOURCES.items():
        cur.execute(sql)
        while True:
            rows = cur.fetchmany(chunk)
            if not rows:
                break
            for r in rows:
                content = rs.blake2b_hex(rs.canonical_enrichment_blob(kind, r))
                payload = rs.enrichment_payload(author, kind, r["entity"],
                                                r["source"], content,
                                                r["fetched_at"])
                pending.append((table, r["id"], rs.sign(payload, key)))
                added += 1
    return added


def run(limit=None, dry_run=False):
    key = load_signing_key(settings)
    if key is None:
        # Callable from post-enrichment hooks — a node without an identity
        # just skips signing, it must not kill the worker thread.
        logger.info("no signing identity (P2P_USERNAME/P2P_PASSWORD) — "
                    "signing skipped")
        return
    author = _pubkey_hex(key)

    conn = psycopg2.connect(settings.database_url)
    conn.cursor_factory = psycopg2.extras.RealDictCursor
    cur = conn.cursor()

    track_ids = sorted(_signable_tracks(cur))
    if limit:
        track_ids = track_ids[:limit]
    logger.info("signable tracks: %d", len(track_ids))

    # pending: (table, pk, signature) collected across all tracks -> one batch
    pending, tracks_touched = [], 0
    for tid in track_ids:
        params = {"tid": tid}
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
            vh = rs.vector_hash(_vector_bytes(seg["vec"]))
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

    enriched = _collect_enrichment(cur, author, key, pending)
    if enriched:
        logger.info("enrichment records to sign: %d", enriched)

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
    if not rs.verify_timestamp(root, ts["date"], ts["ip_hash"], ts["sig"],
                               ts["authority"]):
        conn.rollback()
        raise RuntimeError("Worker timestamp failed verification — aborting")

    cur.execute("""INSERT INTO signing_batches
                     (batch_root, author_pubkey, worker_date, ip_hash,
                      worker_sig, authority)
                   VALUES (%s, %s, %s, %s, %s, %s)""",
                (root, author, ts["date"], ts["ip_hash"], ts["sig"],
                 ts["authority"]))
    # Grouped and batched: a statement per record was fine at audio scale and
    # is not at enrichment scale — the first unified pass seals 300k+ rows.
    by_table: dict = {}
    for (table, pk, sig), proof in zip(pending, proofs):
        by_table.setdefault(table, []).append(
            (pk, author, sig, root, json.dumps(proof)))
    for table, rows in by_table.items():
        psycopg2.extras.execute_values(
            cur,
            f"""UPDATE {table} AS t
                SET author_pubkey = v.author, signature = v.sig,
                    batch_root = v.root, merkle_proof = v.proof::jsonb
                FROM (VALUES %s) AS v(id, author, sig, root, proof)
                WHERE t.id = v.id""",
            rows,
            template="(%s::int, %s, %s, %s, %s)",
            page_size=500,
        )
        logger.info("sealed %d rows in %s", len(rows), table)
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

    cur.execute("SELECT batch_root, worker_date, ip_hash, worker_sig, authority "
                "FROM signing_batches")
    batches = {
        b["batch_root"]: (
            b["worker_date"].astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            str(b["ip_hash"]), b["worker_sig"], b["authority"])
        for b in cur.fetchall()
    }

    ok, bad, bad_samples = 0, 0, []

    def _seal_ok(payload, r):
        b = batches.get(r["batch_root"])
        return bool(b and rs.verify_seal(
            payload, r["signature"], r["author_pubkey"], r["merkle_proof"],
            r["batch_root"], b[0], b[1], b[2], b[3], TRUSTED_AUTHORITIES))

    # Server-side cursor: a signed library is hundreds of thousands of
    # segments, each row carrying a 512-float vector as text — fetchall()
    # here was an OOM kill waiting for the library to grow (it did: the tool
    # worked at 3.7k records and was killed at 452k).
    seg_cur = conn.cursor(name="verify_segments",
                          cursor_factory=psycopg2.extras.RealDictCursor)
    seg_cur.itersize = 2000
    seg_cur.execute("""SELECT s.author_pubkey, s.signature, s.merkle_proof, s.batch_root,
                          e.track_id::text tid, e.model_id::text model,
                          s.segment_index idx, s.vector::text vec,
                          p.pcm_hash, p.chromaprint, p.duration_seconds,
                          p.grid_version
                   FROM embedding_segments s
                   JOIN embeddings e ON e.id = s.embedding_id
                   LEFT JOIN analysis_sources p ON p.id = e.analysis_source_id
                   WHERE s.signature IS NOT NULL""")
    for r in seg_cur:
        if r["pcm_hash"] is None:
            bad += 1
            if len(bad_samples) < 5:
                bad_samples.append(f"segment {r['tid'][:8]}#{r['idx']} UNLINKED")
            continue
        vh = rs.vector_hash(_vector_bytes(r["vec"]))
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
    seg_cur.close()
    logger.info("segments checked: %d valid, %d invalid", ok, bad)

    feat_cur = conn.cursor(name="verify_features",
                           cursor_factory=psycopg2.extras.RealDictCursor)
    feat_cur.itersize = 2000
    feat_cur.execute(f"""SELECT a.author_pubkey, a.signature, a.merkle_proof, a.batch_root,
                           a.track_id::text tid, a.analysis_version,
                           {', '.join('a.' + c for c in FEATURE_ORDER)},
                           p.pcm_hash, p.chromaprint, p.duration_seconds
                    FROM audio_features a
                    LEFT JOIN analysis_sources p ON p.id = a.analysis_source_id
                    WHERE a.signature IS NOT NULL""")
    for r in feat_cur:
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
    feat_cur.close()

    logger.info("seal verification: %d valid, %d invalid", ok, bad)
    if bad_samples:
        logger.warning("invalid: %s", bad_samples)
    return bad == 0


def resign_timestamps():
    """Re-timestamp existing batches under the Worker's current format.

    Author signatures and Merkle roots depend on neither the IP nor the date,
    so re-submitting a root refreshes ONLY the Worker countersignature — the
    per-record seals (488k rows) are untouched, and the Worker preserves each
    root's original date, so authorship priority never regresses.

    Every batch is re-submitted and the WORKER decides what to refresh (it
    versions both the timestamp payload and the ip_hash derivation): a root
    already in the current format comes back byte-identical, so this stays
    idempotent without the client having to know which formats exist."""
    conn = psycopg2.connect(settings.database_url)
    conn.cursor_factory = psycopg2.extras.RealDictCursor
    cur = conn.cursor()

    cur.execute("SELECT batch_root FROM signing_batches ORDER BY created_at")
    roots = [r["batch_root"] for r in cur.fetchall()]
    if not roots:
        logger.info("no batches to re-timestamp")
        return
    logger.info("re-timestamping %d batch(es)", len(roots))

    for root in roots:
        ts = _timestamp_root(root)
        if not rs.verify_timestamp(root, ts["date"], ts["ip_hash"], ts["sig"],
                                   ts["authority"]):
            conn.rollback()
            raise RuntimeError(f"re-timestamp failed verification: {root[:16]}")
        cur.execute("""UPDATE signing_batches
                       SET worker_date=%s, ip_hash=%s, worker_sig=%s, authority=%s
                       WHERE batch_root=%s""",
                    (ts["date"], ts["ip_hash"], ts["sig"], ts["authority"], root))
    conn.commit()
    logger.info("re-timestamped %d batch(es)", len(roots))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--verify", action="store_true",
                    help="re-verify the seal of every signed record; sign nothing")
    ap.add_argument("--resign", action="store_true",
                    help="re-timestamp existing batches under the current format "
                         "(adds ip_hash); signs no new records")
    a = ap.parse_args()
    if a.verify:
        sys.exit(0 if verify_all() else 1)
    if a.resign:
        resign_timestamps()
        sys.exit(0)
    run(limit=a.limit, dry_run=a.dry_run)
