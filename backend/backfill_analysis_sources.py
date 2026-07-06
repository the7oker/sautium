#!/usr/bin/env python3
"""Backfill analysis_sources (pcm_hash + chromaprint) for owned analyzed tracks.

One-time content-address pass after the M1 migration: for every
is_analysis_source media_file whose analysis rows (embeddings /
audio_features) point at it via the legacy source_media_file_id and are not
yet linked to an analysis_sources row, compute the native-PCM BLAKE2b and the
AcoustID fingerprint, insert the source row and link the analysis rows.
After this pass the M2 migration drops the legacy source_* columns.

Selection is an anti-join (already-linked files drop out), so the run is
resumable by construction; the cursor file only spares re-scanning on
restart. Rows whose analysis points at a DIFFERENT file than the current
analysis source are deliberately skipped — the post-M2 staleness predicate
re-analyzes them from the right file, which computes their provenance then.

    docker exec sautium-backend python /app/backfill_analysis_sources.py \
        [--workers 5] [--limit N] [--ids 1,2,3] [--reset]
"""

import argparse
import hashlib
import logging
import os
import subprocess
import sys
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import psycopg2
import psycopg2.extras

from config import settings

CURSOR_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "backfill_analysis_sources.cursor")
FAILED_FILE = CURSOR_FILE.replace(".cursor", ".failed")
DB_BATCH = 500

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("backfill_analysis_sources")


def _load_cursor() -> int:
    try:
        with open(CURSOR_FILE) as f:
            return int(f.read().strip())
    except (FileNotFoundError, ValueError):
        return 0


def _save_cursor(mf_id: int) -> None:
    with open(CURSOR_FILE, "w") as f:
        f.write(str(mf_id))


def _log_failed(mf_id: int, file_path: str, reason: str) -> None:
    with open(FAILED_FILE, "a") as f:
        f.write(f"{mf_id}\t{file_path}\t{reason}\n")


def _fingerprint(file_path: str):
    """Thread worker: (pcm_hash, chromaprint) for one file.

    The PCM stream is hashed chunk-wise — a hi-res long-form track decodes to
    >1 GB of f32 and several workers run concurrently, so buffering whole
    tracks (as sign_audio's one-shot did for 284 files) would OOM here.
    """
    local_path = settings.translate_to_local_path(file_path)
    h = hashlib.blake2b(digest_size=32)
    proc = subprocess.Popen(
        ["ffmpeg", "-v", "error", "-i", local_path, "-f", "f32le", "pipe:1"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    total = 0
    while chunk := proc.stdout.read(1 << 20):
        h.update(chunk)
        total += len(chunk)
    proc.stdout.close()
    err = proc.stderr.read().decode(errors="replace").strip()
    proc.stderr.close()
    if proc.wait(timeout=60) != 0:
        raise RuntimeError(f"ffmpeg rc={proc.returncode}: {err[:200]}")
    if total == 0:
        raise ValueError("ffmpeg produced no samples")

    chromaprint = None
    try:
        out = subprocess.run(["fpcalc", "-plain", local_path],
                             capture_output=True, text=True, timeout=120,
                             check=True)
        chromaprint = out.stdout.strip() or None
    except Exception as e:
        logger.warning("fpcalc failed for %s: %s", local_path, e)
    return h.hexdigest(), chromaprint


def _fetch_batch(conn, after_id: int, ids, batch: int):
    where = "mf.is_analysis_source AND mf.id > %(after)s"
    params: dict = {"after": after_id, "batch": batch}
    if ids:
        where += " AND mf.id = ANY(%(ids)s)"
        params["ids"] = ids
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(f"""
            SELECT mf.id, mf.track_id::text AS track_id, mf.file_path,
                   mf.sample_rate, mf.bit_depth, mf.is_lossless
            FROM media_files mf
            WHERE {where}
              AND NOT EXISTS (SELECT 1 FROM analysis_sources s
                              WHERE s.track_id = mf.track_id
                                AND s.media_file_id = mf.id)
              AND (EXISTS (SELECT 1 FROM embeddings e
                           WHERE e.track_id = mf.track_id
                             AND e.source_media_file_id = mf.id
                             AND e.analysis_source_id IS NULL)
                   OR EXISTS (SELECT 1 FROM audio_features a
                              WHERE a.track_id = mf.track_id
                                AND a.source_media_file_id = mf.id
                                AND a.analysis_source_id IS NULL))
            ORDER BY mf.id
            LIMIT %(batch)s
        """, params)
        return cur.fetchall()


def _store(conn, row, pcm_hash: str, chromaprint) -> None:
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO analysis_sources
                (track_id, origin, media_file_id, pcm_hash, chromaprint,
                 sample_rate, bit_depth, is_lossless)
            VALUES (%(t)s, 'local', %(m)s, %(ph)s, %(fp)s, %(sr)s, %(bd)s, %(ll)s)
            ON CONFLICT (track_id, pcm_hash) DO UPDATE
               SET origin = 'local',
                   media_file_id = EXCLUDED.media_file_id,
                   chromaprint = COALESCE(analysis_sources.chromaprint,
                                          EXCLUDED.chromaprint)
            RETURNING id
        """, {"t": row["track_id"], "m": row["id"], "ph": pcm_hash,
              "fp": chromaprint, "sr": row["sample_rate"],
              "bd": row["bit_depth"], "ll": row["is_lossless"]})
        sid = cur.fetchone()[0]
        cur.execute("""
            UPDATE embeddings SET analysis_source_id = %(sid)s
            WHERE track_id = %(t)s AND source_media_file_id = %(m)s
              AND analysis_source_id IS NULL
        """, {"sid": sid, "t": row["track_id"], "m": row["id"]})
        cur.execute("""
            UPDATE audio_features SET analysis_source_id = %(sid)s
            WHERE track_id = %(t)s AND source_media_file_id = %(m)s
              AND analysis_source_id IS NULL
        """, {"sid": sid, "t": row["track_id"], "m": row["id"]})
    conn.commit()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--workers", type=int, default=5,
                    help="concurrent ffmpeg/fpcalc lanes")
    ap.add_argument("--limit", type=int, default=None,
                    help="stop after N files (subset test)")
    ap.add_argument("--ids", type=str, default=None,
                    help="comma-separated media_files.id list (ignores cursor)")
    ap.add_argument("--reset", action="store_true", help="drop the cursor")
    args = ap.parse_args()

    ids = [int(x) for x in args.ids.split(",")] if args.ids else None
    if args.reset and os.path.exists(CURSOR_FILE):
        os.remove(CURSOR_FILE)
        logger.info("Cursor reset")

    cursor = 0 if ids else _load_cursor()
    if cursor:
        logger.info("Resuming after media_files.id %d", cursor)

    conn = psycopg2.connect(settings.database_url)
    stats = {"processed": 0, "stored": 0, "failed": 0}
    t0 = time.time()

    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM media_files mf WHERE mf.is_analysis_source "
                    "AND NOT EXISTS (SELECT 1 FROM analysis_sources s "
                    "WHERE s.track_id = mf.track_id AND s.media_file_id = mf.id)")
        remaining = cur.fetchone()[0]
    logger.info("~%d unhashed analysis-source files (linkable subset of these "
                "gets processed)", remaining)

    prefetch = args.workers * 2
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        while True:
            batch = DB_BATCH
            if args.limit:
                batch = min(batch, args.limit - stats["processed"])
                if batch <= 0:
                    break
            rows = _fetch_batch(conn, cursor, ids, batch)
            if not rows:
                break
            row_iter = iter(rows)
            pending = deque()

            def _prefetch():
                row = next(row_iter, None)
                if row is not None:
                    pending.append((row, pool.submit(_fingerprint,
                                                     row["file_path"])))

            for _ in range(prefetch):
                _prefetch()
            while pending:
                row, fut = pending.popleft()
                stats["processed"] += 1
                try:
                    pcm_hash, chromaprint = fut.result()
                    _prefetch()
                    _store(conn, row, pcm_hash, chromaprint)
                    stats["stored"] += 1
                except Exception as e:
                    _prefetch()
                    conn.rollback()
                    stats["failed"] += 1
                    _log_failed(row["id"], row["file_path"], str(e)[:300])
                    logger.warning("FAILED mf=%d %s: %s",
                                   row["id"], row["file_path"], e)
                cursor = row["id"]
                if not ids:
                    _save_cursor(cursor)
                if stats["processed"] % 100 == 0:
                    rate = stats["processed"] / (time.time() - t0)
                    eta_h = (remaining - stats["processed"]) / rate / 3600 if rate else 0
                    logger.info("%d/%d done (%.1f/min, ETA %.1fh, %d failed)",
                                stats["processed"], remaining, rate * 60,
                                eta_h, stats["failed"])

    logger.info("DONE in %.1f min: %s", (time.time() - t0) / 60, stats)
    if stats["failed"]:
        logger.warning("failures logged to %s — review before M2 and set the "
                       "coverage allowance accordingly", FAILED_FILE)


if __name__ == "__main__":
    main()
