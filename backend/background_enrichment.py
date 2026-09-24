"""Background enrichment loop — since 2026-09-05 the ONLY path that fetches
metadata from public APIs (Last.fm, lrclib/genius). Started/stopped by the
`enrichment.background_enabled` user_settings flag (settings.py); the
toggle in More → Sync & P2P flips the same key.

Each pass, in order:
  1. Lyrics (lrclib/genius) for tracks missing them
  2. Last.fm artist bios for new artists
  3. Last.fm genre wiki for new genres
  4. Last.fm similars for engaged artists (owned file OR completed listen)
  then the text half of the analysis, over what the steps above just wrote:
  5. Text embeddings (BGE-M3) for owned tracks
  6. Lyrics embeddings
  7. Artist-bio + genre-wiki embeddings
  then, on the interval timer only:
  8. Canonize the uncanonized residue (local MB dump, DB-only)
  9. Missing-album reconcile for canonized artists (local MB dump, DB-only)
 10. name_latin backfill for pre-0a phantom rows (pure Python)

Listening statistics are not fetched here any more: since 2026-09-20 they
come from the ListenBrainz statistics dump (lb_dump_load) or arrive as
signed per-artist slices over P2P (desktop/p2p/lb_slice_cycle).

Steps 5-7 moved here on 2026-09-09. They used to belong to the manual
"Analyse library" run, which meant a person had to authorise embedding a
bio this loop had fetched itself — and had no way to know it was owed: the
first node to be asked was sitting on 7.3k unembedded bios. Nothing else
computes these vectors and no peer carries them, so the producer drains
them. The manual run keeps the AUDIO phase, which only a new file creates.

Steps 5-7 YIELD WHILE THE NODE IS PLAYING (_playback_hold): the model is
the one part of this loop heavy enough to be heard, and HQPlayer wants the
machine more than we do. What the meter cannot see is load from OUTSIDE
this process tree — a game, a compile, HQPlayer's own CPU on the host side
of a Docker install. Playback is the signal that stands in for it, because
it is the one the product can observe on every runtime.

Two cadences. The network and model steps DRAIN: a step that came back with
a full batch has a longer queue behind it, so the next pass follows at once
instead of after the interval — a fresh library's bios take minutes, not
days, while the per-call delay still caps the request rate. A short batch
(queue empty, source unavailable, cooldown) hands control back to the
interval timer, which is the backoff. The premise is that every processed
row LEFT the queue; a step whose failed rows stay queued (the lyrics step's
`errors`, the model steps' `failed`) says so and never drains on them — see
_wants_more. The DB-only steps never drain: each is a bounded slice of a
table walk that the timer paces.

The loop ran behind the P2P sync from 2026-09-05 to 2026-09-22 — the first
pass waited for the first `sautium_sync_done`, every later one woke a pass
— while bios, stats and similars travelled between nodes. Since the Last.fm
layer became node-local (2026-09-19) nothing a sync imports is this loop's
input, so the wait and the wake went: the interval, the drain, the playback
falling edge and the canon wake are the cadence.

One-shot per entity: an artist or genre already in its table, marked
`not_found`/`error` in `external_metadata`, or one this process's own code
failed on (lastfm.internal_failures — retried by the next process, never by
the same code) is skipped.
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
_LYRICS_PER_BATCH = 50            # lrclib/genius calls per batch
_ARTISTS_PER_BATCH = 30           # Last.fm artist.getInfo calls per batch
_GENRES_PER_BATCH = 20            # Last.fm tag.getInfo calls per batch
_SIMILAR_PER_BATCH = 25           # Last.fm artist.getSimilar calls per batch (engaged artists) — the cap that drains a radio-night's phantom qualifications at a bounded rate

_CANONIZE_PER_BATCH = 50          # uncanonized artists distilled per batch (Layer 2: content + phantom)
_DISCOGRAPHY_PER_BATCH = 50       # canonized artists reconciled per batch (local MB dump — DB-only)
_DISCOGRAPHY_STALE_DAYS = 30      # re-sync an artist's discography at most monthly

_NAME_LATIN_PER_BATCH = 50000     # phantom name_latin rows per batch (Phase 0a) — pure-Python transliteration, not API-bound, so far larger than the Last.fm steps

# The text half of the analysis. Model-bound, not API-bound, so the caps are
# batch sizes rather than rate limits — and the encoder is a process-wide
# singleton, so a drain pays its load once and not once per pass. Sized off
# measured throughput on a warm 4090: ~36 texts/s, so a pass spends tens of
# seconds on the GPU, not minutes. _LOCAL_MODEL_SCALE cuts them where that
# arithmetic does not hold.
_TEXT_EMB_PER_BATCH   = 500       # BGE-M3 over owned-track metadata
_LYRICS_EMB_PER_BATCH = 200       # a lyric is chunked into several vectors
_ENRICH_EMB_PER_BATCH = 500       # artist bios + genre wikis, chunked the same way
_LOCAL_MODEL_SCALE    = {"lite": 5}   # profile → batch divisor; CPU encoding is ~an order slower

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
        "lyrics": 0,
        "artists": 0,
        "genres": 0,
        "similar": 0,
        "discography": 0,
        "embeddings": 0,
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


# A wake that lands while a pass is running stays set, so the next wait()
# returns at once and the following pass sees what the waker produced.
_pass_wake = threading.Event()


def wake(reason: str = "") -> None:
    """Run the next pass now instead of at the interval (any thread)."""
    logger.info(f"Background enrichment wake: {reason or 'requested'}")
    _pass_wake.set()


# The DB-only steps run on the interval timer — except when something just
# created their work: a canon run that canonized artists leaves
# discographies to reconcile, and that reconcile is what mints the phantom
# albums the P2P sync then fills. Set by main._canon_trigger_worker.
_db_wake = threading.Event()


def wake_db_steps(reason: str = "") -> None:
    """Run the DB-only steps at the next pass, and start that pass now."""
    logger.info(f"Background enrichment DB-steps wake: {reason or 'requested'}")
    _db_wake.set()
    _pass_wake.set()


def _sleep_until(seconds: int) -> None:
    """Idle until the interval elapses, a wake lands or cancel — 1s slices
    so stop() never waits out the interval."""
    deadline = time.time() + seconds
    while time.time() < deadline and not _cancel_flag():
        if _pass_wake.wait(timeout=1):
            _pass_wake.clear()
            return


# ============================================================
# Per-batch steps
# ============================================================

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

    Every row counted in `processed` left the queue: a bio row, a marker,
    or — for a failure in our own code — this process's exclusion list
    (lastfm.internal_failures). An unavailable source ends the batch with
    the row unmarked, for the next pass.
    """
    from lastfm import LastFmService, internal_failures, note_internal_failure

    stats = {"processed": 0, "success": 0, "not_found": 0, "errors": 0}
    lastfm = LastFmService()

    # Most-listened first: a Last.fm history import engages hundreds of
    # artists at once, and the ones the owner actually plays are the bios
    # worth having first. The weight comes from the small side
    # (local_play_stats), not a per-artist probe.
    sql = text(f"""
        WITH weight AS (
            SELECT ta.artist_id, sum(lps.total_listen_time) AS listened
              FROM local_play_stats lps
              JOIN track_artists ta ON ta.track_id = lps.track_id AND ta.role = 'primary'
             GROUP BY ta.artist_id
        )
        SELECT a.id, a.name
        FROM artists a
        LEFT JOIN weight w ON w.artist_id = a.id
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
        AND a.id <> ALL(CAST(:skip AS uuid[]))
        ORDER BY w.listened DESC NULLS LAST, a.name
        LIMIT :batch
    """)

    with get_db_context() as db:
        rows = db.execute(sql, {"batch": int(limit),
                                "skip": internal_failures("artist")}).fetchall()
        if not rows:
            return stats
        logger.info(f"Background: {len(rows)} artists queued for Last.fm bio")

        for row in rows:
            if _cancel_flag():
                break
            try:
                result = lastfm.enrich_artist(db, row.id, row.name)
            except Exception as e:
                logger.error(f"Background artist failed for {row.name}: {e}", exc_info=True)
                db.rollback()
                note_internal_failure("artist", row.id)
                stats["errors"] += 1
            else:
                if result["status"] == "unavailable":
                    logger.info("Background artists: Last.fm unavailable — ending batch")
                    stats["unavailable"] = True
                    break
                if result["status"] == "success":
                    stats["success"] += 1
                elif result["status"] == "not_found":
                    stats["not_found"] += 1
                else:
                    stats["errors"] += 1
            stats["processed"] += 1

    return stats


def _step_missing_genres(limit: int) -> Dict[str, int]:
    """Fetch Last.fm wiki text for genres without a description. Same row
    contract as _step_missing_artists."""
    from lastfm import LastFmService, internal_failures, note_internal_failure

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
        AND g.id <> ALL(CAST(:skip AS uuid[]))
        ORDER BY g.name
        LIMIT :batch
    """)

    with get_db_context() as db:
        rows = db.execute(sql, {"batch": int(limit),
                                "skip": internal_failures("genre")}).fetchall()
        if not rows:
            return stats
        logger.info(f"Background: {len(rows)} genres queued for Last.fm wiki")

        for row in rows:
            if _cancel_flag():
                break
            try:
                result = lastfm.enrich_genre(db, row.id, row.name)
            except Exception as e:
                logger.error(f"Background genre failed for {row.name}: {e}", exc_info=True)
                db.rollback()
                note_internal_failure("genre", row.id)
                stats["errors"] += 1
            else:
                status = result["status"]
                if status == "unavailable":
                    logger.info("Background genres: Last.fm unavailable — ending batch")
                    stats["unavailable"] = True
                    break
                if status == "success":
                    stats["success"] += 1
                elif status == "not_found":
                    stats["not_found"] += 1
                else:
                    stats["errors"] += 1
            stats["processed"] += 1

    return stats


def _step_similar_artists(limit: int) -> Dict[str, int]:
    """Fetch Last.fm similar-artist lists for engaged artists that never had
    them: owned artists OR phantoms with a completed listen. Delegates to
    lastfm.backfill_similar — one source of truth for the candidate rule
    (shared with manual force=True refreshes)."""
    from lastfm import backfill_similar

    try:
        return backfill_similar(limit=int(limit), cancel_flag=_cancel_flag)
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
    #  gated on the Last.fm cooldown, stats keys whose rows STAY queued —
    #  see _wants_more)
    ("lyrics",      _step_lyrics,          _LYRICS_PER_BATCH,      "found",   False, ("errors",)),
    ("artists",     _step_missing_artists, _ARTISTS_PER_BATCH,     "success", True,  ()),
    ("genres",      _step_missing_genres,  _GENRES_PER_BATCH,      "success", True,  ()),
    # Engagement-gated similars (owned file OR completed listen) that never
    # had getSimilar. Last of the network steps, so the stubs it mints go
    # straight into canonize when the DB steps follow in the same pass.
    ("similar",     _step_similar_artists, _SIMILAR_PER_BATCH,     "stored",  True,  ()),
)


def _playback_hold() -> Optional[str]:
    """Why the text half must yield right now, or None.

    Playback only — the same rule the identity miner already follows
    (load_meter.mining_hold). Playback is a priority signal, not a load:
    when the machine is doing the thing the product exists for, discretionary
    work gets out of the way. HQPlayer is the hungriest process on a listening
    machine, and a few hundred texts through BGE-M3 while it feeds a DAC is
    how a background nicety turns into a stutter.

    Deliberately NOT gated on headroom, for the reason load_meter states for
    mining: headroom counts OUR OWN process tree, so a step that pauses on it
    pauses on its own CPU and oscillates. Foreign load is a different
    question and this meter cannot answer it — see the module docstring."""
    from desktop.p2p import load_meter
    meter = load_meter.current()
    if meter is not None and meter.playback_active:
        return "playback"
    return None


def _model_cancel() -> bool:
    """Stop signal for the model steps: the loop's own cancel, or playback
    starting mid-batch. The generators stamp every batch they finish, so
    yielding here costs the current batch's tail and nothing else."""
    return _cancel_flag() or _playback_hold() is not None


def _step_text_embeddings(limit: int) -> Dict[str, int]:
    from text_embeddings import generate_text_embeddings
    return generate_text_embeddings(limit=limit, cancel_flag=_model_cancel)


def _step_lyrics_embeddings(limit: int) -> Dict[str, int]:
    from lyrics_embeddings import generate_lyrics_embeddings
    return generate_lyrics_embeddings(limit=limit, cancel_flag=_model_cancel)


def _step_enrichment_embeddings(limit: int) -> Dict[str, int]:
    """Artist bios + genre wikis. The generator caps EACH of the two, so the
    pair reports up to 2 x limit and the drain check reads it as one batch —
    over-eager by at most one extra pass, which the next short batch ends."""
    from enrichment_embeddings import generate_all_enrichment_embeddings
    out: Dict[str, int] = {}
    for part in generate_all_enrichment_embeddings(
            limit=limit, cancel_flag=_model_cancel).values():
        for key, value in part.items():
            if isinstance(value, int):
                out[key] = out.get(key, 0) + value
    return out


_LOCAL_MODEL_STEPS = (
    # (state key, step, batch cap)
    ("text_emb",   _step_text_embeddings,       _TEXT_EMB_PER_BATCH),
    ("lyrics_emb", _step_lyrics_embeddings,     _LYRICS_EMB_PER_BATCH),
    ("enrich_emb", _step_enrichment_embeddings, _ENRICH_EMB_PER_BATCH),
)


def _wants_more(stats: Dict[str, int], limit: int,
                stuck: Tuple[str, ...] = ()) -> bool:
    """A full batch means the queue behind it is longer than the batch, so
    the next pass follows at once; a short one (queue drained, source
    unavailable, cancel) hands control back to the timer.

    The premise is that every processed row LEFT the queue, or the next
    pass drains on the same rows forever. The Last.fm steps guarantee it: a
    written row, a not_found/error marker, a `last_similar_sync` stamp, or
    — for a failure in our own code — lastfm.internal_failures, which their
    candidate queries exclude. `stuck` names the stats keys of a step that
    cannot: the lyrics step's `errors` (a failed track keeps its place) and
    the model steps' `failed` (an unembedded track likewise). A batch
    holding any of those waits out the interval — the timer is the backoff
    from a failing source or model. Reading `errors` for every step was
    what let one bug of ours, uncached and alphabetically first, end every
    artist batch and hold the step to a batch per interval."""
    if stats.get("processed", 0) < limit:
        return False
    return not any(stats.get(key) for key in stuck)


def _run_network_steps() -> Tuple[Dict[str, Any], bool]:
    """One batch of every network step, in order. Returns the per-step
    stats and whether any step still has a backlog behind it."""
    summary: Dict[str, Any] = {}
    backlog = False
    lastfm_down = False
    for key, step, limit, total_key, lastfm_gated, stuck in _NETWORK_STEPS:
        if _cancel_flag():
            break
        # One verdict on Last.fm per pass: a step that found it unavailable
        # spares the steps behind it their own retries.
        if lastfm_gated and (lastfm_down or cooling_down('lastfm')):
            summary[key] = {}
            continue
        _set(current_step=key)
        stats = step(limit)
        summary[key] = stats
        _bump(key, stats.get(total_key, 0))
        backlog = backlog or _wants_more(stats, limit, stuck)
        if lastfm_gated and stats.get("unavailable"):
            lastfm_down = True
    return summary, backlog


def _run_local_model_steps() -> Tuple[Dict[str, Any], bool]:
    """The text half of the analysis: embeddings for track metadata, lyrics,
    artist bios and genre wikis.

    It belongs here because it is work THIS loop creates. The bio was fetched
    two steps up, the lyric one step up; no peer carries these vectors (they
    are not sync categories) and nothing else computes them. Before the loop
    drained them, the pool grew silently until a human happened to press
    "Analyse library" — 7.3k unembedded bios on the first node that looked,
    and no way for its owner to know. Asking a person to authorise work the
    machine created and knows how to do is not a decision, it is a chore.

    Model-bound rather than network-bound, but the same drain contract: a
    full batch means more behind it, and the encoder is a process-wide
    singleton (text_embedder.get_text_embedder), so consecutive passes pay
    the load once. A node with no ML runtime has nothing to run at all."""
    summary: Dict[str, Any] = {}
    import hardware_profile
    profile = hardware_profile.resolve()
    if not profile.ml_available:
        return summary, False
    hold = _playback_hold()
    if hold:
        # No backlog reported: the pass hands control back to the timer, and
        # the falling edge of playback wakes it sooner (see _loop).
        logger.debug("text half yields: %s", hold)
        return {"text_half": {"held": hold}}, False
    # The profile governs compute (CLAUDE.md): lite encodes on the CPU,
    # where the same slice costs an order of magnitude more wall-clock.
    # Smaller slices there — same queue, more passes, a machine that stays
    # usable while it drains.
    scale = _LOCAL_MODEL_SCALE.get(profile.name, 1)

    backlog = False
    for key, step, cap in _LOCAL_MODEL_STEPS:
        if _cancel_flag():
            break
        limit = max(20, cap // scale)
        _set(current_step=key)
        stats = step(limit)
        summary[key] = stats
        _bump("embeddings", stats.get("success", 0))
        backlog = backlog or _wants_more(stats, limit, ("failed",))
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


def _wake_when_playback_ends() -> None:
    """Run a pass as soon as the machine stops playing.

    Without this the text half would hold and then wait out the whole
    interval, so a node played in half-hour stretches would embed almost
    nothing. The meter fires on band changes; only the playback falling edge
    is a reason to wake, and only after a rise we actually saw."""
    from desktop.p2p import load_meter
    meter = load_meter.current()
    if meter is None:
        return
    seen_playing = {"v": False}

    def _on_load(snap: Dict[str, Any]) -> None:
        if snap.get("playback"):
            seen_playing["v"] = True
        elif seen_playing["v"]:
            seen_playing["v"] = False
            wake("playback ended")

    meter.subscribe(_on_load)


def _loop() -> None:
    """One pass = a batch of every network step, then — when the interval
    timer says so — the DB-only steps. A pass that left a network backlog
    behind is followed by the next one at once (drain); otherwise the loop
    idles until the interval elapses or a wake lands. Exits when cancel is
    set."""
    logger.info("Background enrichment loop started")
    _wake_when_playback_ends()
    db_due_at = 0.0        # the first pass runs the DB steps too
    minted = False         # canon work created since the last NOTIFY
    grew = False           # new phantom gaps created since the last sync request
    try:
        while not _cancel_flag():
            backlog = False
            db_ran = False
            try:
                batch, backlog = _run_network_steps()
                # After the network steps, so a bio or lyric fetched in this
                # pass is embedded in this pass.
                if not _cancel_flag():
                    local, local_backlog = _run_local_model_steps()
                    batch.update(local)
                    backlog = backlog or local_backlog
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

            # Wake the Library channel: this pass changed what the UI reads
            # from it — the Sync screen's background-enrichment block, and
            # the guidance trail, which has to learn that the pool of work
            # grew without anyone touching the library. A producer that
            # mutates state and tells nobody is exactly what forces a client
            # onto a timer. notify_* swallows a closed subscriber loop.
            from routers.settings import notify_library_subscribers
            notify_library_subscribers()

            # The DB steps shed seals (canon re-keys) and mint tracklists
            # under already-signed tracks — those need the album layer
            # rescanned end to end. The network steps write nothing signable
            # since the Last.fm layer became node-local (2026-09-19), so
            # only a pass with DB steps is a producer.
            if db_ran:
                import notary
                notary.wake("background pass", full=True)

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
