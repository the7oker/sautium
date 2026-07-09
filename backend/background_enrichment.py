"""Background (network-only) enrichment loop.

Periodic batch loop that fetches metadata from public APIs only — no
GPU work. Started/stopped by the `enrichment.background_enabled`
user_settings flag (settings.py); the toggle in More → Sync & P2P
flips the same key.

Each batch, in order:
  1. Last.fm track_stats (listeners + playcount) — priority-ordered
  2. Lyrics (lrclib/genius) for tracks missing them
  3. Last.fm artist bios for new artists
  4. Last.fm genre wiki for new genres
  5. Missing-album reconcile for canonized artists (local MB dump, DB-only)

Track-stats priority (Tier 1 → 3):
  1. Tracks whose primary artist has the most accumulated listen-time
  2. Tracks whose genre has the most accumulated listen-time
  3. Everything else

One-shot per entity: a track already in `track_stats` (source='lastfm')
or marked `not_found`/`error` in `external_metadata` is skipped. Same
pattern for artists/genres against their respective tables.
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

from sqlalchemy import text

from api_cooldown import cooling_down
from database import get_db_context

logger = logging.getLogger(__name__)


# Tunables. These are intentionally module-level constants — there is
# no production reason to make them user-configurable until we have
# data showing the defaults are wrong.
_BATCH_INTERVAL_MIN = 30          # sleep between batches
_TRACK_STATS_PER_BATCH = 100      # Last.fm track.getInfo calls per batch
_LYRICS_PER_BATCH = 50            # lrclib/genius calls per batch
_ARTISTS_PER_BATCH = 30           # Last.fm artist.getInfo calls per batch
_GENRES_PER_BATCH = 20            # Last.fm tag.getInfo calls per batch
_LASTFM_DELAY_S = 0.2             # ~5 req/sec, the Last.fm published limit

_CANONIZE_PER_BATCH = 50          # uncanonized artists distilled per batch (Layer 2: content + phantom)
_DISCOGRAPHY_PER_BATCH = 50       # canonized artists reconciled per batch (local MB dump — DB-only)
_DISCOGRAPHY_STALE_DAYS = 30      # re-sync an artist's discography at most monthly

_NAME_LATIN_PER_BATCH = 50000     # phantom name_latin rows per batch (Phase 0a) — pure-Python transliteration, not API-bound, so far larger than the Last.fm steps

_PRIORITY_SQL = text("""
    WITH artist_listen AS (
        SELECT ta.artist_id, SUM(lh.duration_listened) AS sec
        FROM listening_history lh
        JOIN track_artists ta ON ta.track_id = lh.track_id
        WHERE lh.duration_listened > 0
        GROUP BY ta.artist_id
    ),
    genre_listen AS (
        SELECT ag.genre_id, SUM(lh.duration_listened) AS sec
        FROM listening_history lh
        JOIN LATERAL (
            SELECT DISTINCT ag2.genre_id
            FROM media_files mf
            JOIN album_variants av ON av.id = mf.album_variant_id
            JOIN album_genres ag2 ON ag2.album_id = av.album_id
            WHERE mf.track_id = lh.track_id
        ) ag ON true
        WHERE lh.duration_listened > 0
        GROUP BY ag.genre_id
    ),
    candidates AS (
        SELECT t.id           AS track_id,
               t.title        AS track_title,
               a.name         AS artist_name,
               MAX(al.sec)    AS artist_score,
               MAX(gl.sec)    AS genre_score
        FROM tracks t
        JOIN track_artists ta ON ta.track_id = t.id AND ta.role = 'primary'
        JOIN artists a        ON a.id = ta.artist_id
        LEFT JOIN artist_listen al ON al.artist_id = ta.artist_id
        LEFT JOIN media_files mf_g    ON mf_g.track_id = t.id
        LEFT JOIN album_variants av_g ON av_g.id = mf_g.album_variant_id
        LEFT JOIN album_genres ag     ON ag.album_id = av_g.album_id
        LEFT JOIN genre_listen gl     ON gl.genre_id = ag.genre_id
        -- owned tracks only: phantom tracklist rows (no media_files) must
        -- not burn the Last.fm budget
        WHERE EXISTS (
            SELECT 1 FROM media_files mf WHERE mf.track_id = t.id
        )
        AND NOT EXISTS (
            SELECT 1 FROM track_stats ts
            WHERE ts.track_id = t.id AND ts.source = 'lastfm'
        )
        AND NOT EXISTS (
            SELECT 1 FROM external_metadata em
            WHERE em.entity_type = 'track'
              AND em.entity_id = t.id::text
              AND em.source = 'lastfm'
              AND em.metadata_type = 'stats'
              AND em.fetch_status IN ('not_found', 'error')
        )
        GROUP BY t.id, t.title, a.name
    )
    SELECT track_id, artist_name, track_title,
           COALESCE(artist_score, 0) AS artist_score,
           COALESCE(genre_score, 0)  AS genre_score
    FROM candidates
    ORDER BY artist_score DESC NULLS LAST,
             genre_score  DESC NULLS LAST,
             track_id
    LIMIT :batch
""")


# ============================================================
# Shared state
# ============================================================

_state_lock = threading.Lock()
_state: Dict[str, Any] = {
    "running": False,            # thread alive
    "cancel": False,             # stop requested
    "current_step": "",          # what the loop is doing right now
    "last_run_at": None,         # ISO timestamp of last batch end
    "next_run_at": None,         # ISO timestamp of next batch start
    "last_batch": None,          # counts dict from last run_once
    "total": {                   # cumulative counts since process start
        "track_stats": 0,
        "lyrics": 0,
        "artists": 0,
        "genres": 0,
        "discography": 0,
    },
}
_thread: Optional[threading.Thread] = None


def _set(**kwargs) -> None:
    with _state_lock:
        _state.update(kwargs)


def _bump(key: str, n: int) -> None:
    if n <= 0:
        return
    with _state_lock:
        _state["total"][key] = _state["total"].get(key, 0) + n


def _cancel_flag() -> bool:
    return bool(_state["cancel"])


# ============================================================
# Per-batch steps
# ============================================================

def _step_track_stats(limit: int) -> Dict[str, int]:
    """Fetch Last.fm track stats for priority-ordered tracks.

    Picks tracks whose primary artist / genre has the most accumulated
    listen-time from `listening_history`; falls back to insertion order
    for tracks with no listen history. Per-track rate limit matches
    enrich_artist/enrich_album (0.2s).
    """
    from lastfm import LastFmService

    stats = {"processed": 0, "success": 0, "not_found": 0, "errors": 0}
    lastfm = LastFmService()

    with get_db_context() as db:
        rows = db.execute(_PRIORITY_SQL, {"batch": int(limit)}).fetchall()
        if not rows:
            return stats
        logger.info(f"Background: {len(rows)} tracks queued for Last.fm stats")

        for row in rows:
            if _cancel_flag():
                break
            stats["processed"] += 1
            try:
                result = lastfm.enrich_track(
                    db, row.track_id, row.artist_name, row.track_title,
                )
                if result["status"] == "rate_limited":
                    logger.info("Background track_stats: Last.fm rate-limited — ending batch")
                    break
                if result["status"] == "success":
                    stats["success"] += 1
                elif result["status"] == "not_found":
                    stats["not_found"] += 1
                else:
                    stats["errors"] += 1
            except Exception as e:
                logger.error(
                    f"Background track_stats failed for "
                    f"{row.artist_name} - {row.track_title}: {e}"
                )
                stats["errors"] += 1
                db.rollback()
            time.sleep(_LASTFM_DELAY_S)

    return stats


def _step_lyrics(limit: int) -> Dict[str, int]:
    """Fetch lyrics for tracks missing them. Delegates to the existing batch."""
    from track_enrichment import _fetch_lyrics_batch
    return _fetch_lyrics_batch(limit=limit, cancel_flag=_cancel_flag)


def _step_missing_artists(limit: int) -> Dict[str, int]:
    """Fetch Last.fm bios for artists that don't have one yet."""
    from lastfm import LastFmService

    stats = {"processed": 0, "success": 0, "not_found": 0, "errors": 0}
    lastfm = LastFmService()

    sql = text("""
        SELECT DISTINCT a.id, a.name
        FROM artists a
        JOIN track_artists ta ON ta.artist_id = a.id
        WHERE NOT EXISTS (
            SELECT 1 FROM artist_bios ab
            WHERE ab.artist_id = a.id AND ab.source = 'lastfm'
        )
        AND NOT EXISTS (
            SELECT 1 FROM external_metadata em
            WHERE em.entity_type = 'artist'
              AND em.entity_id = a.id::text
              AND em.source = 'lastfm'
              AND em.metadata_type = 'bio'
              AND em.fetch_status IN ('not_found', 'error')
        )
        ORDER BY a.name
        LIMIT :batch
    """)

    with get_db_context() as db:
        rows = db.execute(sql, {"batch": int(limit)}).fetchall()
        if not rows:
            return stats
        logger.info(f"Background: {len(rows)} artists queued for Last.fm bio")

        for row in rows:
            if _cancel_flag():
                break
            stats["processed"] += 1
            try:
                result = lastfm.enrich_artist(db, row.id, row.name)
                if result["status"] == "rate_limited":
                    logger.info("Background artists: Last.fm rate-limited — ending batch")
                    break
                if result["status"] == "success":
                    stats["success"] += 1
                elif result["status"] == "not_found":
                    stats["not_found"] += 1
                else:
                    stats["errors"] += 1
            except Exception as e:
                logger.error(f"Background artist failed for {row.name}: {e}")
                stats["errors"] += 1
                db.rollback()
            time.sleep(_LASTFM_DELAY_S)

    return stats


def _step_missing_genres(limit: int) -> Dict[str, int]:
    """Fetch Last.fm wiki text for genres without a description."""
    from lastfm import LastFmService

    stats = {"processed": 0, "success": 0, "not_found": 0, "errors": 0}
    lastfm = LastFmService()

    sql = text("""
        SELECT g.id, g.name
        FROM genres g
        WHERE NOT EXISTS (
            SELECT 1 FROM genre_descriptions gd
            WHERE gd.genre_id = g.id AND gd.source = 'lastfm'
        )
        AND NOT EXISTS (
            SELECT 1 FROM external_metadata em
            WHERE em.entity_type = 'genre'
              AND em.entity_id = g.id::text
              AND em.source = 'lastfm'
              AND em.metadata_type = 'description'
              AND em.fetch_status IN ('not_found', 'error')
        )
        ORDER BY g.name
        LIMIT :batch
    """)

    with get_db_context() as db:
        rows = db.execute(sql, {"batch": int(limit)}).fetchall()
        if not rows:
            return stats
        logger.info(f"Background: {len(rows)} genres queued for Last.fm wiki")

        for row in rows:
            if _cancel_flag():
                break
            stats["processed"] += 1
            try:
                result = lastfm.enrich_genre(db, row.id, row.name)
                status = result.get("status")
                if status == "rate_limited":
                    logger.info("Background genres: Last.fm rate-limited — ending batch")
                    break
                if status == "success":
                    stats["success"] += 1
                elif status == "not_found":
                    # enrich_genre does not write external_metadata for
                    # not_found — record it here so the next batch skips
                    # this genre instead of re-asking Last.fm forever.
                    db.execute(text("""
                        INSERT INTO external_metadata (
                            entity_type, entity_id, source,
                            metadata_type, data, fetch_status, error_message
                        ) VALUES (
                            'genre', :gid, 'lastfm',
                            'description', '{}'::jsonb, 'not_found',
                            'No wiki on Last.fm'
                        )
                        ON CONFLICT (entity_type, entity_id, source, metadata_type)
                        DO UPDATE SET fetch_status = 'not_found',
                                      updated_at = CURRENT_TIMESTAMP
                    """), {"gid": str(row.id)})
                    db.commit()
                    stats["not_found"] += 1
                else:
                    stats["errors"] += 1
            except Exception as e:
                logger.error(f"Background genre failed for {row.name}: {e}")
                stats["errors"] += 1
                db.rollback()
            time.sleep(_LASTFM_DELAY_S)

    return stats


def _step_canonize(limit: int) -> Dict[str, int]:
    """Distill the uncanonized residue (Layer 2 entry point): owned artists via
    content overlap, trackless phantoms via genre overlap. Runs before
    discography so freshly-canonized artists get their shelves the same batch."""
    from canon import distill_uncanonized

    try:
        out = distill_uncanonized(limit=int(limit))
    except Exception as e:
        logger.error(f"Background canonize failed: {e}")
        return {"canonized": 0, "errors": 1}
    ph = out.get("phantom") or {}
    out["canonized"] = ph.get("canonized", 0)
    return out


def _step_sync_discographies(limit: int) -> Dict[str, int]:
    """Reconcile missing-album discovery for canonized artists whose data
    is stale (`_DISCOGRAPHY_STALE_DAYS`), in `stale_canonized_artists`
    priority order: listened artists first, then artists similar to them,
    then the rest — relevant shelves refresh first after a dump reload
    un-stamps everyone. With the MB dump loaded this is pure local-DB
    work — no network, no cooldowns; `rate_limited` only fires on
    dump-less nodes (MB HTTP API) and ends the batch early, `mb_loading`
    while a dump reload holds the advisory lock.
    """
    from discography import stale_canonized_artists, sync_artist_discography

    stats = {"processed": 0, "new_albums": 0, "errors": 0}

    rows = stale_canonized_artists(limit=int(limit),
                                   stale_days=_DISCOGRAPHY_STALE_DAYS)
    if not rows:
        return stats
    logger.info(f"Background: {len(rows)} canonized artists queued for discography reconcile")

    for row in rows:
        if _cancel_flag():
            break
        stats["processed"] += 1
        try:
            result = sync_artist_discography(row["id"], row["name"])
            if result.get("status") in ("rate_limited", "mb_loading"):
                logger.info(f"Background discography: {result['status']} — ending batch")
                break
            stats["new_albums"] += result.get("new", 0)
        except Exception as e:
            logger.error(f"Background discography failed for {row['name']}: {e}")
            stats["errors"] += 1

    return stats


# ============================================================
# Loop
# ============================================================

def _step_backfill_name_latin(limit: int) -> Dict[str, int]:
    """Backfill name_latin/title_latin for phantom rows minted before Phase 0a.

    Pure-Python transliteration + DB write (no external API), so it runs in
    large batches. NULL-gated and id-cursored, so it's idempotent and never
    re-scans. Owned rows are handled once by the backfill_name_latin CLI;
    new rows are filled at write time (models event + raw choke-points)."""
    from backfill_name_latin import backfill_phantom, backfill_aliases, backfill_filetag_aliases

    out: Dict[str, int] = {}
    for tbl in ("artists", "albums", "tracks"):
        if _cancel_flag():
            break
        try:
            out[tbl] = backfill_phantom(tbl, limit=limit)
        except Exception as e:
            logger.error(f"name_latin backfill failed for {tbl}: {e}")
            out[tbl] = 0
    if not _cancel_flag():
        try:
            out["aliases"] = backfill_aliases(limit=limit)                   # 0b: CJK alt readings
            out["filetag_aliases"] = backfill_filetag_aliases(limit=limit)   # 0b: human file tags
        except Exception as e:
            logger.error(f"alias backfill failed: {e}")
            out["aliases"] = 0
    return out


def _run_once() -> Dict[str, Any]:
    """Run one full batch (all steps). Honours cancel between steps."""
    summary = {"track_stats": {}, "lyrics": {}, "artists": {},
               "genres": {}, "canonize": {}, "discography": {}, "name_latin": {}}

    if _cancel_flag():
        return summary

    _set(current_step="track_stats")
    if not cooling_down('lastfm'):
        summary["track_stats"] = _step_track_stats(_TRACK_STATS_PER_BATCH)
        _bump("track_stats", summary["track_stats"].get("success", 0))

    if _cancel_flag():
        return summary

    _set(current_step="lyrics")
    summary["lyrics"] = _step_lyrics(_LYRICS_PER_BATCH)
    _bump("lyrics", summary["lyrics"].get("found", 0))

    if _cancel_flag():
        return summary

    _set(current_step="artists")
    if not cooling_down('lastfm'):
        summary["artists"] = _step_missing_artists(_ARTISTS_PER_BATCH)
        _bump("artists", summary["artists"].get("success", 0))

    if _cancel_flag():
        return summary

    _set(current_step="genres")
    if not cooling_down('lastfm'):
        summary["genres"] = _step_missing_genres(_GENRES_PER_BATCH)
        _bump("genres", summary["genres"].get("success", 0))

    if _cancel_flag():
        return summary

    # Layer 2: distill uncanonized artists (content + phantom) BEFORE
    # discography, so a freshly-canonized artist gets its shelf this batch.
    # Defer canon while a dump op is downloading/loading — it would read a stale or
    # half-TRUNCATEd dump and watermark artists out of the dump's own fresh pass.
    try:
        from routers.settings import mb_load_active
        _dump_busy = mb_load_active()
    except Exception:
        _dump_busy = False
    if not _dump_busy:
        from canon import algo_canon
        with algo_canon() as _ok:   # priority over AI; holds the dump lock for the run
            if _ok:
                _set(current_step="canonize")
                summary["canonize"] = _step_canonize(_CANONIZE_PER_BATCH)
                _bump("canonize", summary["canonize"].get("canonized", 0))

    if _cancel_flag():
        return summary

    # NOTE: the AI canonization tier is NOT run on this 30-min timer — it's LLM-
    # slow and re-attempting the same residue every cycle would burn tokens. It is
    # EVENT-triggered instead (once per dump load + once per scan, new files only),
    # async in the background via routers.settings.start_aicanon_job, plus the
    # manual "Run now" button. See the AI assistant screen.

    if _cancel_flag():
        return summary

    # Re-enabled 2026-06-12: discography moved from Deezer to the local MB
    # dump — the "no automatic crawling" reason for the 2026-06-02 disable is
    # gone (reconcile is DB-only; dump-less nodes end the batch on the first
    # MB API rate limit).
    _set(current_step="discography")
    summary["discography"] = _step_sync_discographies(_DISCOGRAPHY_PER_BATCH)
    _bump("discography", summary["discography"].get("new_albums", 0))

    if _cancel_flag():
        return summary

    # Phase 0a: drain pre-0a phantom rows into name_latin so cross-script search
    # reaches them too. Runs last — canonize/discography mint new phantoms (already
    # filled at write time); this catches up the millions minted before 0a landed.
    _set(current_step="name_latin")
    summary["name_latin"] = _step_backfill_name_latin(_NAME_LATIN_PER_BATCH)
    _bump("name_latin", sum(summary["name_latin"].values()))

    # MB canonicalization is NOT a background step — it runs at its real trigger
    # points (post-scan, post-MB-dump-load) against the local dump. The background
    # loop is reserved for the future P2P-carrier canon (a node without the dump
    # asking a carrier peer); add it here when that lands.

    return summary


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _loop() -> None:
    """Top-level loop: run one batch, sleep, repeat. Exits when cancel set."""
    logger.info("Background enrichment loop started")
    try:
        while not _cancel_flag():
            try:
                batch = _run_once()
            except Exception as e:
                logger.error(f"Background batch failed: {e}", exc_info=True)
                batch = {"error": str(e)}

            now = _now_iso()
            next_at = datetime.fromtimestamp(
                time.time() + _BATCH_INTERVAL_MIN * 60, tz=timezone.utc,
            ).isoformat()
            _set(last_batch=batch, last_run_at=now, next_run_at=next_at,
                 current_step="idle")

            # Sleep in 1s slices so cancel doesn't have to wait for
            # the full interval. Plain time.sleep would block stop()
            # for up to half an hour, which is rude.
            slept = 0
            while slept < _BATCH_INTERVAL_MIN * 60:
                if _cancel_flag():
                    break
                time.sleep(1)
                slept += 1
    finally:
        _set(running=False, current_step="", next_run_at=None)
        logger.info("Background enrichment loop stopped")


# ============================================================
# Public API
# ============================================================

def start() -> bool:
    """Start the loop in a daemon thread. No-op if already running."""
    global _thread
    with _state_lock:
        if _state["running"]:
            return False
        _state["running"] = True
        _state["cancel"] = False
        _state["current_step"] = "starting"

    _thread = threading.Thread(
        target=_loop, name="background-enrichment", daemon=True,
    )
    _thread.start()
    logger.info("Background enrichment thread spawned")
    return True


def stop() -> bool:
    """Request loop shutdown. Returns immediately; the thread exits
    within ~1s (between sleep slices). No-op if not running."""
    with _state_lock:
        if not _state["running"]:
            return False
        _state["cancel"] = True
    logger.info("Background enrichment stop requested")
    return True


def is_running() -> bool:
    return bool(_state["running"])


def status() -> Dict[str, Any]:
    """Snapshot of loop state for the UI."""
    with _state_lock:
        return {
            "running":      _state["running"],
            "current_step": _state["current_step"],
            "last_run_at":  _state["last_run_at"],
            "next_run_at":  _state["next_run_at"],
            "last_batch":   _state["last_batch"],
            "totals":       dict(_state["total"]),
            "interval_min": _BATCH_INTERVAL_MIN,
        }
