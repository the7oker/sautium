"""Background enrichment loop — since 2026-09-05 the ONLY path that fetches
metadata from public APIs (Last.fm, lrclib/genius); the manual "Analyse
library" run is GPU/text analysis only. No GPU work here. Started/stopped
by the `enrichment.background_enabled` user_settings flag (settings.py);
the toggle in More → Sync & P2P flips the same key.

Each pass, in order:
  1. Last.fm track_stats (listeners + playcount) — priority-ordered
  2. Lyrics (lrclib/genius) for tracks missing them
  3. Last.fm artist bios for new artists
  4. Last.fm genre wiki for new genres
  5. Last.fm similars for engaged artists (owned file OR completed listen)
  then, on the interval timer only:
  6. Canonize the uncanonized residue (local MB dump, DB-only)
  7. Missing-album reconcile for canonized artists (local MB dump, DB-only)
  8. name_latin backfill for pre-0a phantom rows (pure Python)

Two cadences. The network steps DRAIN: a step that came back with a full,
error-free batch has a longer queue behind it, so the next pass follows at
once instead of after the interval — a fresh library's bios take minutes,
not days, while the per-call delay still caps the request rate. A short
batch (queue empty, rate limit, cooldown) or any error hands control back
to the interval timer, which is the backoff. The DB-only steps never
drain: each is a bounded slice of a table walk that the timer paces.

The loop follows the P2P sync rather than its own clock where it can: at
boot the first pass waits (bounded) for the first `sautium_sync_done`,
and every later sync completion wakes a pass at once — peers fill a fresh
library's gaps for free, the pass that follows fetches only the rest.
The interval is the fallback for a node with sync off or no peers.

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
from typing import Any, Callable, Dict, List, Optional, Tuple

from sqlalchemy import text

from api_cooldown import cooling_down
from database import get_db_context
from sql_queries import ARTIST_ENGAGED

logger = logging.getLogger(__name__)


# How long a negative verdict from the source keeps an entity out of the
# planner. It expires because none of these are permanent facts: a Last.fm
# HTTP 500 is a bad minute, and an artist absent today gets added tomorrow —
# without a window the first bad answer we ever got about an entity is the
# last answer we ever accept.
#
# No attempt counter and no exponential curve: a repeated failure refreshes
# updated_at, so the window itself already spaces the retries.
_NEGATIVE_CACHE_WINDOW = """
    AND em.updated_at > NOW() - (CASE em.fetch_status
                                     WHEN 'not_found' THEN INTERVAL '90 days'
                                     ELSE INTERVAL '7 days' END)
"""


# Tunables. These are intentionally module-level constants — there is
# no production reason to make them user-configurable until we have
# data showing the defaults are wrong.
_BATCH_INTERVAL_MIN = 30          # idle sleep between passes; also the DB-only steps' cadence
_DB_RETRY_S = 120                 # DB-only steps with work left (full batch, or blocked by a dump/slice load): retry soon, not next cycle
_FIRST_SYNC_WAIT_MIN = 10         # boot: how long the first pass waits for the first P2P sync
_TRACK_STATS_PER_BATCH = 100      # Last.fm track.getInfo calls per batch
_LYRICS_PER_BATCH = 50            # lrclib/genius calls per batch
_ARTISTS_PER_BATCH = 30           # Last.fm artist.getInfo calls per batch
_GENRES_PER_BATCH = 20            # Last.fm tag.getInfo calls per batch
_SIMILAR_PER_BATCH = 25           # Last.fm artist.getSimilar calls per batch (engaged artists) — the cap that drains a radio-night's phantom qualifications at a bounded rate
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
              AND em.fetch_status IN ('not_found', 'error')""" + _NEGATIVE_CACHE_WINDOW + """
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
    "draining": False,           # network backlog: passes follow at once, no interval sleep
    "last_run_at": None,         # ISO timestamp of last batch end
    "next_run_at": None,         # ISO timestamp of next batch start
    "last_batch": None,          # counts dict from last run_once
    "total": {                   # cumulative counts since process start
        "track_stats": 0,
        "lyrics": 0,
        "artists": 0,
        "genres": 0,
        "similar": 0,
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


# Cross-process wake from the launcher's P2P sync: the LISTEN thread in
# routers/settings.py sets it on NOTIFY sautium_sync_done. A sync that
# completes while a pass is running leaves it set, so the next wait()
# returns at once and the following pass sees the freshly imported rows.
_sync_wake = threading.Event()


def wake(reason: str = "") -> None:
    """Run the next pass now instead of at the interval (any thread)."""
    logger.info(f"Background enrichment wake: {reason or 'requested'}")
    _sync_wake.set()


# The DB-only steps run on the interval timer — except when something just
# created their work: a canon run that canonized artists leaves
# discographies to reconcile, and that reconcile is what mints the phantom
# albums the P2P sync then fills. Set by main._canon_trigger_worker.
_db_wake = threading.Event()


def wake_db_steps(reason: str = "") -> None:
    """Run the DB-only steps at the next pass, and start that pass now."""
    logger.info(f"Background enrichment DB-steps wake: {reason or 'requested'}")
    _db_wake.set()
    _sync_wake.set()


def _await_first_sync() -> None:
    """Boot order: let the first P2P sync fill the gaps for free before the
    first API call — a fresh library's bios, stats and similars mostly exist
    on peers already. Bounded: a node with sync off, or one whose peers are
    slow, runs its first pass after _FIRST_SYNC_WAIT_MIN anyway."""
    try:
        from routers.settings import _read
        p2p_on = bool(_read("sync.p2p_enabled"))
    except Exception as e:
        logger.warning(f"sync.p2p_enabled read failed — not waiting for a sync: {e}")
        p2p_on = False
    if not p2p_on:
        return
    _set(current_step="awaiting_sync")
    deadline = time.time() + _FIRST_SYNC_WAIT_MIN * 60
    while time.time() < deadline and not _cancel_flag():
        if _sync_wake.wait(timeout=1):
            _sync_wake.clear()
            logger.info("Background enrichment: first pass follows the P2P sync")
            return
    logger.info(
        f"Background enrichment: no P2P sync within {_FIRST_SYNC_WAIT_MIN} min, "
        "first pass runs now"
    )


def _sleep_until(seconds: int) -> None:
    """Idle until the interval elapses, a sync completes (wake) or cancel —
    1s slices so stop() never waits out the interval."""
    deadline = time.time() + seconds
    while time.time() < deadline and not _cancel_flag():
        if _sync_wake.wait(timeout=1):
            _sync_wake.clear()
            return


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
    """Fetch Last.fm bios for artists that don't have one yet.

    Per-artist fan-out, so it is gated on what a human or the discovery
    graph actually touched: an owned file, a completed listen, an MB anchor
    (canonized), a similar-artist edge, a streaming mint. "Has a track" is
    not enough since 2026-08-25 — the phantom tracklist mint credits every
    slot to its own artist (a compilation is ~15 of them), and on a dump
    node that is a quarter-million name-only stubs nobody asked about; one
    Last.fm call each is the blowup the engagement rule exists to prevent.
    """
    from lastfm import LastFmService

    stats = {"processed": 0, "success": 0, "not_found": 0, "errors": 0}
    lastfm = LastFmService()

    sql = text(f"""
        SELECT a.id, a.name
        FROM artists a
        WHERE (
            {ARTIST_ENGAGED}
            OR EXISTS (SELECT 1 FROM artist_mbids am WHERE am.artist_id = a.id)
            OR EXISTS (SELECT 1 FROM similar_artists sa
                       WHERE sa.artist_id = a.id OR sa.similar_artist_id = a.id)
        )
        AND NOT EXISTS (
            SELECT 1 FROM artist_bios ab
            WHERE ab.artist_id = a.id AND ab.source = 'lastfm'
        )
        AND NOT EXISTS (
            SELECT 1 FROM external_metadata em
            WHERE em.entity_type = 'artist'
              AND em.entity_id = a.id::text
              AND em.source = 'lastfm'
              AND em.metadata_type = 'bio'
              AND em.fetch_status IN ('not_found', 'error')""" + _NEGATIVE_CACHE_WINDOW + """
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
              AND em.fetch_status IN ('not_found', 'error')""" + _NEGATIVE_CACHE_WINDOW + """
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


def _step_similar_artists(limit: int) -> Dict[str, int]:
    """Fetch Last.fm similar-artist lists for engaged artists that never had
    them: owned artists OR phantoms with a completed listen. Delegates to
    lastfm.backfill_similar — one source of truth for the candidate rule
    (shared with manual force=True refreshes)."""
    from lastfm import backfill_similar

    try:
        return backfill_similar(limit=int(limit), delay=_LASTFM_DELAY_S,
                                cancel_flag=_cancel_flag)
    except Exception as e:
        logger.error(f"Background similar backfill failed: {e}")
        return {"processed": 0, "stored": 0, "errors": 1}


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
    # A full batch on either layer means more residue behind it — a fresh
    # node lands hundreds of slice-fed phantoms at once.
    if (ph.get("phantoms", 0) >= limit
            or (out.get("content") or {}).get("pending", 0) >= limit):
        out["retry_soon"] = True
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

    # `retry_soon`: work is left — the step could not do it (API cooldown,
    # a dump or slice load holding the lock) or a full batch says more is
    # queued — so the loop comes back in _DB_RETRY_S instead of leaving
    # the reconcile to the next interval. A fresh node canonizes hundreds
    # of artists in minutes; at 50 per interval their shelves took hours.
    stats = {"processed": 0, "new_albums": 0, "errors": 0}

    # Dump-less nodes hit the MB HTTP API — skip the whole step while it's
    # cooling down (no-op on dump nodes, which never arm the cooldown).
    if cooling_down('musicbrainz'):
        stats["retry_soon"] = True
        return stats

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
                stats["retry_soon"] = True
                break
            stats["new_albums"] += result.get("new", 0)
        except Exception as e:
            logger.error(f"Background discography failed for {row['name']}: {e}")
            stats["errors"] += 1

    # Drain only while the batch still carried WANTED artists (listened,
    # seeded, similar to listened). Tier 3 — every other canonized artist —
    # is the ungated fan-out: each reconciled discography credits more
    # artists, whose canon mints more names, whose discographies credit
    # more. Its only bound is the interval, so it keeps it: 50 per 30 min,
    # never 50 per 2 min (a fresh node went 21k → 94k phantom tracks in 40
    # minutes when it did).
    if (stats["processed"] >= int(limit)
            and any(int(r.get("tier", 3)) < 3 for r in rows)):
        stats["retry_soon"] = True
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


_NETWORK_STEPS = (
    # (state key, step, batch cap, stats key folded into the running totals,
    #  gated on the Last.fm cooldown)
    ("track_stats", _step_track_stats,     _TRACK_STATS_PER_BATCH, "success", True),
    ("lyrics",      _step_lyrics,          _LYRICS_PER_BATCH,      "found",   False),
    ("artists",     _step_missing_artists, _ARTISTS_PER_BATCH,     "success", True),
    ("genres",      _step_missing_genres,  _GENRES_PER_BATCH,      "success", True),
    # Engagement-gated similars (owned file OR completed listen) that never
    # had getSimilar. Last of the network steps, so the stubs it mints go
    # straight into canonize when the DB steps follow in the same pass.
    ("similar",     _step_similar_artists, _SIMILAR_PER_BATCH,     "stored",  True),
)


def _wants_more(stats: Dict[str, int], limit: int) -> bool:
    """A full, error-free batch means the queue behind it is longer than the
    batch, so the next pass follows at once. A short batch (queue drained,
    rate limit, cancel) or any error hands control back to the timer — the
    interval IS the backoff. Safe because every step stamps what it
    processed (a row, a not_found/error marker, last_similar_sync): a full
    batch never hands the same rows back."""
    return stats.get("processed", 0) >= limit and not stats.get("errors")


def _run_network_steps() -> Tuple[Dict[str, Any], bool]:
    """One batch of every network step, in order. Returns the per-step
    stats and whether any step still has a backlog behind it."""
    summary: Dict[str, Any] = {}
    backlog = False
    for key, step, limit, total_key, lastfm_gated in _NETWORK_STEPS:
        if _cancel_flag():
            break
        if lastfm_gated and cooling_down('lastfm'):
            summary[key] = {}
            continue
        _set(current_step=key)
        stats = step(limit)
        summary[key] = stats
        _bump(key, stats.get(total_key, 0))
        backlog = backlog or _wants_more(stats, limit)
    return summary, backlog


def _run_db_steps() -> Dict[str, Any]:
    """The DB-only steps — canonize, discography reconcile, name_latin
    backfill. Timer-paced regardless of network backlog: each is a bounded
    slice of a table walk, not a queue with an end."""
    summary: Dict[str, Any] = {}
    if _cancel_flag():
        return summary

    # Layer 2: distill uncanonized artists (content + phantom) BEFORE
    # discography, so a freshly-canonized artist gets its shelf this pass.
    # Defer canon while a dump op is downloading/loading — it would read a stale or
    # half-TRUNCATEd dump and watermark artists out of the dump's own fresh pass.
    try:
        from routers.settings import mb_load_active
        _dump_busy = mb_load_active()
    except Exception:
        _dump_busy = False
    if _dump_busy:
        summary["retry_soon"] = True
    else:
        from canon import algo_canon
        with algo_canon() as _ok:   # priority over AI; holds the dump lock for the run
            if _ok:
                _set(current_step="canonize")
                summary["canonize"] = _step_canonize(_CANONIZE_PER_BATCH)
                _bump("canonize", summary["canonize"].get("canonized", 0))
                if summary["canonize"].get("retry_soon"):
                    summary["retry_soon"] = True

    if _cancel_flag():
        return summary

    # NOTE: the AI canonization tier is NOT run on this timer — it's LLM-slow
    # and re-attempting the same residue every cycle would burn tokens. It is
    # EVENT-triggered instead (once per dump load + once per scan, new files
    # only), async in the background via routers.settings.start_aicanon_job,
    # plus the manual "Run now" button. See the AI assistant screen.

    # Re-enabled 2026-06-12: discography moved from Deezer to the local MB
    # dump — the "no automatic crawling" reason for the 2026-06-02 disable is
    # gone (reconcile is DB-only; dump-less nodes end the batch on the first
    # MB API rate limit).
    _set(current_step="discography")
    summary["discography"] = _step_sync_discographies(_DISCOGRAPHY_PER_BATCH)
    _bump("discography", summary["discography"].get("new_albums", 0))
    if summary["discography"].get("retry_soon"):
        summary["retry_soon"] = True

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
    """One pass = a batch of every network step, then — when the interval
    timer says so — the DB-only steps. A pass that left a network backlog
    behind is followed by the next one at once (drain); otherwise the loop
    idles until the interval elapses or a P2P sync completes. Exits when
    cancel is set."""
    logger.info("Background enrichment loop started")
    db_due_at = 0.0        # the first pass runs the DB steps too
    minted = False         # canon work created since the last NOTIFY
    grew = False           # new phantom gaps created since the last sync request
    try:
        _await_first_sync()
        while not _cancel_flag():
            backlog = False
            db_ran = False
            try:
                batch, backlog = _run_network_steps()
                if not _cancel_flag() and (time.time() >= db_due_at
                                           or _db_wake.is_set()):
                    _db_wake.clear()
                    batch.update(_run_db_steps())
                    db_due_at = time.time() + (
                        _DB_RETRY_S if batch.get("retry_soon")
                        else _BATCH_INTERVAL_MIN * 60)
                    db_ran = True
            except Exception as e:
                logger.error(f"Background batch failed: {e}", exc_info=True)
                batch = {"error": str(e)}

            next_at = None if backlog else datetime.fromtimestamp(
                time.time() + _BATCH_INTERVAL_MIN * 60, tz=timezone.utc,
            ).isoformat()
            _set(last_batch=batch, last_run_at=_now_iso(), next_run_at=next_at,
                 draining=backlog)

            # Every network step writes signable rows (bios, tags, similars,
            # stats, genre descriptions); the DB steps also shed seals (canon
            # re-keys) and mint tracklists under already-signed tracks — those
            # need the album layer rescanned end to end.
            import notary
            notary.wake("background pass", full=db_ran)

            # A pass that minted artists (similars, streaming stubs) just
            # created canon work that a dump-less node can only do with
            # peer slices. Tell the launcher instead of letting the freshly
            # minted names sit out its 6-hour timer — at most once per DB
            # cycle while draining, not once per pass: the launcher's slice
            # fetch has no dedupe of its own.
            minted = minted or bool((batch.get("artists") or {}).get("success")
                                    or (batch.get("similar") or {}).get("stored"))
            # New phantom albums (discography reconcile on the MB dump) and
            # new similar-artist stubs are gaps the P2P sync has never asked
            # about — on a fresh node a seed's 500 tracks become 20k within
            # minutes. Ask for a sync now rather than at the interval; the
            # launcher merges and serialises concurrent requests.
            grew = grew or bool((batch.get("discography") or {}).get("new_albums")
                                or (batch.get("similar") or {}).get("stored"))
            if (minted or grew) and (db_ran or not backlog):
                try:
                    from db_pool import db_execute
                    if minted:
                        db_execute("NOTIFY sautium_enrich_done")
                    if grew:
                        db_execute("NOTIFY sautium_sync_request")
                except Exception as e:
                    logger.debug(f"enrich notify failed: {e}")
                minted = grew = False

            if backlog:
                continue

            _set(current_step="idle")
            _sleep_until(_BATCH_INTERVAL_MIN * 60)
    finally:
        _set(running=False, current_step="", next_run_at=None, draining=False)
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
            "draining":     _state["draining"],
            "last_run_at":  _state["last_run_at"],
            "next_run_at":  _state["next_run_at"],
            "last_batch":   _state["last_batch"],
            "totals":       dict(_state["total"]),
            "interval_min": _BATCH_INTERVAL_MIN,
        }
