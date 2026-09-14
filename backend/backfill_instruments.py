#!/usr/bin/env python3
"""Full-track backfill: instruments (windowed max) + amplitude librosa features.

Round 2 of the instruments refactor. Round 1 stored raw top-20 scores from a
single 10s window; this pass re-tags with ensemble_instruments.tag_full_track
(10s windows every STRIDE_SECONDS, max per label — catches the sax solo two
minutes past the middle and the loud act of a multi-act track) and, since the
whole track is decoded anyway, recomputes the amplitude block
(audio_analysis.amplitude_features) over the full 22050Hz signal instead of
the middle 30s. bpm/key/key_confidence and the CLAP-derived columns are NOT
touched.

Resumable: walks media_files.id ascending, cursor persisted after every
track. Deterministic done-predicate between restarts = the cursor file;
losing it just re-runs from the start, which is idempotent (same audio →
same scores). Per-track error isolation: failures land in the .failed file
and the walk continues.

Run inside the backend container:
    docker exec sautium-backend python /app/backfill_instruments.py \
        [--limit N] [--ids 1,2,3] [--reset]
"""

import argparse
import json
import logging
import os
import sys
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import librosa
import psycopg2
import psycopg2.extras

from audio_analysis import ANALYSIS_VERSION, amplitude_features, load_full_track_48k
from config import settings

CURSOR_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "backfill_instruments.cursor")
FAILED_FILE = CURSOR_FILE.replace(".cursor", ".failed")
DB_BATCH = 500
# A decoded whole track is big (30min @ 48kHz mono f32 = 345MB; Schulze goes
# to 79min = 910MB) — bound the in-flight decode queue by both count and
# total duration or piled-up Future results OOM-kill the container (exit
# 137, learned the hard way with an unbounded queue).
PREFETCH = 2
PREFETCH_BUDGET_SECONDS = 40 * 60
DECODE_SR = 48000
LIBROSA_SR = 22050

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("backfill_instruments")


def _load_cursor() -> int:
    try:
        with open(CURSOR_FILE) as f:
            return int(f.read().strip())
    except (FileNotFoundError, ValueError):
        return 0


def _save_cursor(mf_id: int) -> None:
    with open(CURSOR_FILE, "w") as f:
        f.write(str(mf_id))


def _log_failed(mf_id: int, reason: str) -> None:
    with open(FAILED_FILE, "a") as f:
        f.write(f"{mf_id}\t{reason}\n")


def _decode_and_amp(file_path: str, cue_start=None, cue_end=None):
    """Thread worker: decode the WHOLE track once (shared scanner decode
    path) + compute the amplitude block on the same 22050Hz signal the
    scanner uses. Returns (audio_48k, amp_features)."""
    local_path = settings.translate_to_local_path(file_path)
    audio_48k = load_full_track_48k(local_path, cue_start, cue_end)
    y22 = librosa.resample(audio_48k, orig_sr=DECODE_SR, target_sr=LIBROSA_SR)
    return audio_48k, amplitude_features(y22, LIBROSA_SR)


def _fetch_batch(conn, after_id: int, ids: list[int] | None, batch: int):
    where = "mf.is_analysis_source AND mf.id > %(after)s"
    params: dict = {"after": after_id, "batch": batch}
    if ids:
        where += " AND mf.id = ANY(%(ids)s)"
        params["ids"] = ids
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(f"""
            SELECT mf.id, mf.track_id::text AS track_id, mf.file_path,
                   mf.cue_start_seconds, mf.cue_end_seconds,
                   COALESCE(mf.duration_seconds, 300) AS duration_seconds
            FROM media_files mf
            JOIN audio_features af ON af.track_id = mf.track_id
            WHERE {where}
            ORDER BY mf.id
            LIMIT %(batch)s
        """, params)
        return cur.fetchall()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=None,
                    help="stop after N tracks (subset test)")
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

    from device import get_device
    from instrument_tagger import get_instrument_tagger
    tagger = get_instrument_tagger(get_device())

    conn = psycopg2.connect(settings.database_url)
    stats = {"processed": 0, "updated": 0, "failed": 0}
    t0 = time.time()

    with ThreadPoolExecutor(max_workers=2) as pool:
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
            queued = [None]   # one-row lookahead held back by the budget

            def _prefetch():
                if queued[0] is None:
                    queued[0] = next(row_iter, None)
                row = queued[0]
                if row is None:
                    return
                in_flight = sum(float(r["duration_seconds"]) for r, _ in pending)
                if pending and in_flight + float(row["duration_seconds"]) > PREFETCH_BUDGET_SECONDS:
                    return   # retry after the current head is consumed
                queued[0] = None
                pending.append((row, pool.submit(_decode_and_amp,
                                                 row["file_path"],
                                                 row["cue_start_seconds"],
                                                 row["cue_end_seconds"])))

            for _ in range(PREFETCH):
                _prefetch()
            while pending:
                row, fut = pending.popleft()
                stats["processed"] += 1
                try:
                    audio_48k, amp = fut.result()
                    _prefetch()
                    tags = tagger.tag_full_track(audio_48k)
                    with conn.cursor() as cur:
                        cur.execute(
                            """UPDATE audio_features
                               SET instruments = %s::jsonb,
                                   energy = %s, energy_db = %s,
                                   dynamic_range_db = %s, brightness = %s,
                                   zero_crossing_rate = %s,
                                   analysis_version = %s,
                                   updated_at = CURRENT_TIMESTAMP
                               WHERE track_id = %s::uuid""",
                            (json.dumps(tags), amp["energy"], amp["energy_db"],
                             amp["dynamic_range_db"], amp["brightness"],
                             amp["zero_crossing_rate"], ANALYSIS_VERSION,
                             row["track_id"]))
                    conn.commit()
                    stats["updated"] += 1
                except Exception as e:
                    conn.rollback()
                    stats["failed"] += 1
                    _log_failed(row["id"], f"{type(e).__name__}: {e}")
                    logger.warning("mf %d failed: %s", row["id"], e)
                    _prefetch()
                cursor = row["id"]
                if not ids:
                    _save_cursor(cursor)
                if stats["processed"] % 100 == 0:
                    rate = stats["processed"] / (time.time() - t0)
                    logger.info("processed=%d updated=%d failed=%d "
                                "rate=%.2f/s cursor=%d",
                                stats["processed"], stats["updated"],
                                stats["failed"], rate, cursor)

    conn.close()
    logger.info("DONE %s in %.1f min", stats, (time.time() - t0) / 60)


if __name__ == "__main__":
    main()
