"""
Settings endpoints — single-roundtrip read of everything the Settings
screen needs (library stats + scan state, AI provider/model/auth/usage,
sync & P2P preferences, HQPlayer connection summary), plus PUT/POST
endpoints to mutate the user-controlled bits.

Preferences live in user_settings (JSONB K/V) so the values survive
across backend restarts and are sync-able through P2P in the future.
The scan/enrich actions are thin wrappers around the existing top-
level /scan/* and /enrich/* endpoints so the Settings UI does not
need to know about them directly.
"""

import asyncio
import json
import logging
import select
import threading
from typing import Any, Dict, List, Optional, Tuple

import psycopg2
import psycopg2.extensions

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import text

from config import settings as app_settings
from database import get_db_context
from db_pool import db_execute, db_query, db_query_one

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/settings", tags=["settings"])


# ============================================================
# SSE: live updates for the Library screen
# ============================================================
#
# Workers (scan / enrich) mutate _scan_state / _enrich_state in
# main.py. Whenever they reach a meaningful checkpoint they call
# notify_library_subscribers() from this module. Each subscriber is
# an (asyncio.Event, loop) pair that the SSE endpoint registers when
# a client connects. Workers wake every subscriber via
# loop.call_soon_threadsafe(evt.set). Client then re-fetches
# /api/settings/library to read the fresh state. Same shape as the
# /api/p2p/chat/stream pattern — wake-event, not payload queue, so
# workers don't pay serialization cost on every progress tick.

_library_sse_clients: List[Tuple[asyncio.Event, asyncio.AbstractEventLoop]] = []
_library_sse_lock = threading.Lock()

# Mirror channel for the AI > Claude Code screen. Wake events fire
# from the install worker thread when state transitions (running →
# done / error) so the Web UI re-fetches /api/settings/ai/claude/state
# without polling. Same wake-event pattern as the library channel.
_claude_sse_clients: List[Tuple[asyncio.Event, asyncio.AbstractEventLoop]] = []
_claude_sse_lock = threading.Lock()

# AI-canonization run: a background thread resolves the residue, pushing batch
# progress + the AI's reasoning to the More→AI screen over the same wake-event
# SSE pattern. Single-flight (one run at a time); state is read via GET /ai.
_aicanon_sse_clients: List[Tuple[asyncio.Event, asyncio.AbstractEventLoop]] = []
_aicanon_sse_lock = threading.Lock()
_aicanon: Dict[str, Any] = {
    "running": False, "total": 0, "processed": 0, "canonized": 0,
    "skipped": 0, "guard_rejected": 0, "errors": 0, "model": None, "items": [],
}
_aicanon_lock = threading.Lock()


def _notify_aicanon() -> None:
    with _aicanon_sse_lock:
        for evt, loop in list(_aicanon_sse_clients):
            try:
                loop.call_soon_threadsafe(evt.set)
            except RuntimeError:
                continue


def _aicanon_should_run() -> bool:
    """Whether an event trigger (dump load / scan) may auto-start AI canon: a
    provider is authed AND the tier is enabled (free default On, paid Off unless
    the user opted in). The dump presence is enforced inside ai_canonize_stream."""
    st = _ai_state()
    c = st.get("canonization") or {}
    # also defer to a dump op's data phases (mb_load_active) — AI must not read a
    # half-TRUNCATEd dump (the dump's own post-load AI trigger fires in its
    # 'canonicalizing' phase, where mb_load_active is already False).
    return bool(c.get("available")) and bool(c.get("enabled")) and not mb_load_active()


def start_aicanon_job(since=None, *, force: bool = False) -> bool:
    """Single-flight: start the background AI-canon worker. `since` scopes to
    new-file artists (post-scan). `force` bypasses the enabled/provider gate (the
    UI 'Run now'); event triggers pass force=False so a paid/disabled tier stays
    quiet. Returns True if a run was started."""
    if not force and not _aicanon_should_run():
        return False
    with _aicanon_lock:
        if _aicanon["running"]:
            return False
        _aicanon.update(running=True, total=0, processed=0, canonized=0,
                        skipped=0, guard_rejected=0, errors=0, items=[])
    threading.Thread(target=_aicanon_worker, args=(None, since), daemon=True).start()
    _notify_aicanon()
    return True


def _aicanon_worker(limit: Optional[int], since=None) -> None:
    from canon.ai_canon import ai_canonize_stream
    try:
        for ev in ai_canonize_stream(limit=limit, dry_run=False, since=since):
            with _aicanon_lock:
                if ev["event"] == "start":
                    _aicanon.update(running=True, total=ev["total"], model=ev.get("model"))
                elif ev["event"] == "batch":
                    _aicanon.update(processed=ev["processed"], canonized=ev["canonized"],
                                    skipped=ev["skipped"], guard_rejected=ev["guard_rejected"],
                                    errors=ev["errors"])
                    # newest first, keep a bounded reasoning log for the UI
                    _aicanon["items"] = (ev["items"] + _aicanon["items"])[:80]
                elif ev["event"] == "done":
                    _aicanon.update(running=False, skipped_reason=ev.get("skipped"))
            _notify_aicanon()
    except Exception:
        logging.getLogger(__name__).exception("ai_canon worker crashed")
    finally:
        with _aicanon_lock:
            _aicanon["running"] = False
        _notify_aicanon()


def notify_library_subscribers() -> None:
    """Thread-safe wake of every connected Library SSE client.
    Workers call this after start / phase transition / completion."""
    with _library_sse_lock:
        for evt, loop in list(_library_sse_clients):
            try:
                loop.call_soon_threadsafe(evt.set)
            except RuntimeError:
                # Loop closed; subscriber will clean itself up on the
                # next iteration of its generator's finally block.
                continue


def notify_claude_subscribers() -> None:
    """Thread-safe wake of every AI > Claude Code SSE client. Called
    from the install worker thread on state transitions."""
    with _claude_sse_lock:
        for evt, loop in list(_claude_sse_clients):
            try:
                loop.call_soon_threadsafe(evt.set)
            except RuntimeError:
                continue


# Sync DB listener: bridge between launcher's P2PManager and the
# library SSE channel. The launcher fires NOTIFY sautium_sync_done
# after each P2P sync (manual or auto). We listen here, then wake
# library subscribers so the Settings screen re-fetches and shows
# the new last_sync_at / items_received. Same NOTIFY pattern used
# by chat (routers/p2p.py); no polling.

_sync_listener_thread: Optional[threading.Thread] = None
_sync_listener_running = False


def _sync_db_listener() -> None:
    """Background thread: LISTEN sautium_sync_done, wake library SSE."""
    while _sync_listener_running:
        conn = None
        try:
            conn = psycopg2.connect(app_settings.database_url)
            conn.set_isolation_level(
                psycopg2.extensions.ISOLATION_LEVEL_AUTOCOMMIT
            )
            with conn.cursor() as cur:
                cur.execute("LISTEN sautium_sync_done")

            while _sync_listener_running:
                ready = select.select([conn], [], [], 5)
                if ready[0]:
                    conn.poll()
                    if conn.notifies:
                        while conn.notifies:
                            conn.notifies.pop(0)
                        notify_library_subscribers()
        except Exception as e:
            logger.debug(f"Sync DB listener error: {e}")
            if _sync_listener_running:
                import time
                time.sleep(1)
        finally:
            if conn:
                try:
                    conn.close()
                except Exception:
                    pass


def start_sync_listener() -> None:
    global _sync_listener_thread, _sync_listener_running
    if _sync_listener_thread and _sync_listener_thread.is_alive():
        return
    _sync_listener_running = True
    _sync_listener_thread = threading.Thread(
        target=_sync_db_listener, daemon=True, name="sync-sse-listener"
    )
    _sync_listener_thread.start()


def stop_sync_listener() -> None:
    global _sync_listener_running
    _sync_listener_running = False


# ============================================================
# Preference keys + defaults (single source of truth)
# ============================================================

# user_settings JSONB stores one row per key, value can be any JSON.
# Defaults are returned to the UI when the row is missing so first-
# run users get a sensible Settings screen with no prior writes.
_DEFAULTS: Dict[str, Any] = {
    "sync.p2p_enabled":          True,
    "sync.auto_interval_min":    30,
    # Size of the rare-artist DHT announce tail (0/null = node key only).
    "sync.announce_limit":       300,
    # Foreign TRACKS whose audio analysis this node will hold on other
    # peers' behalf (push-seeding). A node behind CGNAT cannot be pulled
    # FROM, so its own analysis only reaches the network if somebody
    # carries it. ~46 KB per track measured; 0/null = don't carry.
    # Mirrored by sync_queries.CARRY_DEFAULT_BUDGET (the launcher's sync
    # server reads user_settings directly and needs the same fallback) —
    # keep in step.
    "sync.carry_limit":          2000,
    "enrichment.background_enabled": True,
    # Relay for others (Phase D): a reachable non-lite node announces
    # Sautium-cap:relay and carries wake subscriptions + live forwards for
    # CGNAT-bound strangers (voucher-authorized). Traffic is chat frames —
    # tiny; the default is on because a relay layer that nobody opts into
    # never cold-starts.
    "p2p.relay_enabled": True,
    # Owned tracks whose analysis arrived over P2P: False = keep the
    # imported copy (no GPU re-spend on tracks that already work), True =
    # re-derive first-hand from the local file (matches this rip exactly,
    # makes the node an author for those tracks, and is the network's
    # poison detector). Read by embeddings.py / audio_analysis.py pending
    # queries.
    "enrichment.reanalyze_imported": False,
    # Provider/model default to None so the first-run UI shows
    # "Not selected" instead of pretending Claude is picked when the
    # wizard offered an explicit "no AI" option.
    "ai.provider":               None,
    "ai.model":                  None,
    "ai.api_key":                None,
    # HQPlayer connection — env defaults still apply at first boot;
    # user_settings overrides them on PUT /api/settings/hqplayer.
    "hqplayer.host":             None,
    "hqplayer.port":             None,
    # Artist-screen Albums block sort. release_year is the default;
    # other valid values: time_listened, popularity, recently_added,
    # a_z. UI exposes this through a bottom-sheet picker on the
    # Albums section header.
    "albums.sort":               "release_year",
    # last sync metadata — written by the sync runner when a cycle
    # completes; surfaced in the "Last sync · N new items" row.
    "sync.last_at":              None,
    "sync.last_items_received":  None,
    # MusicBrainz local dump — optional auxiliary layer for artist
    # canonicalization. version/last-update written by the loader.
    "musicbrainz.auto_update":   False,
    "musicbrainz.last_update_at": None,
    # AI canonicalization (the judgment tier). null = use the computed default
    # (On when the AI provider is free/subscription, Off when it's a paid API key).
    "ai.canonization_enabled":   None,
    # Playback output selection (Output picker). null type = legacy resolution:
    # HQPlayer when an endpoint is configured, otherwise no active output.
    "output.type":               None,   # 'hqplayer' | 'local' | 'dlna'
    "output.local_device":       None,   # "{hostapi}::{device name}"
    "output.local_exclusive":    False,  # WASAPI exclusive / bit-perfect claim
    "output.dlna_renderer":      None,   # {udn, location, name} of the picked renderer
    # Stream quality for DLNA / This device (browser) — outdoor bandwidth
    # saving. Lossless is the default and the everyday mode; the Opus tiers
    # are a deliberate metered-connection choice. HQPlayer / local always
    # lossless (they never carry the quality param into the media URL).
    "output.stream_quality":     "lossless",   # lossless | opus_192 | opus_96
}


# Defaults that depend on the hardware profile — computed at read time so
# the UI and the startup autostart see the same value. An explicit
# user_settings row always wins.
def _background_enrichment_default() -> bool:
    import hardware_profile
    return hardware_profile.resolve().background_enrichment_default


_DYNAMIC_DEFAULTS = {
    "enrichment.background_enabled": _background_enrichment_default,
}


def _read(key: str) -> Any:
    row = db_query_one("SELECT value FROM user_settings WHERE key = %(k)s", {"k": key})
    if row is None:
        dyn = _DYNAMIC_DEFAULTS.get(key)
        return dyn() if dyn else _DEFAULTS.get(key)
    return row.get("value")


def _write(key: str, value: Any) -> None:
    db_execute(
        """
        INSERT INTO user_settings (key, value) VALUES (%s, %s::jsonb)
        ON CONFLICT (key) DO UPDATE
            SET value = EXCLUDED.value,
                updated_at = CURRENT_TIMESTAMP
        """,
        (key, json.dumps(value)),
    )


# ============================================================
# MusicBrainz local dump (optional auxiliary layer)
# ============================================================

# In-memory progress for the (single) running download/load job. Surfaced in
# the Library payload and woken over the shared library SSE channel.
_mb_state: Dict[str, Any] = {"running": False, "phase": "", "progress": "",
                             "pct": None, "error": None}
_mb_lock = threading.Lock()


def mb_load_active() -> bool:
    """True while an MB-dump op is DOWNLOADING / LOADING (the data-mutating phases),
    so scan/background/AI canon DEFER — otherwise they read a stale or half-TRUNCATEd
    dump and watermark those artists out of the dump's fresh post-load canon. False
    during the dump's own 'canonicalizing' phase, so the dump worker's OWN post-load
    canon + AI trigger (which run THEN) are not blocked by their own op."""
    with _mb_lock:
        return bool(_mb_state["running"]) and _mb_state["phase"] not in ("canonicalizing", "done", "")


def _mb_progress(update: Dict) -> None:
    """Loader callback → human progress line + numeric pct (None = indeterminate)
    for the bar + SSE wake."""
    phase = update.get("phase")
    pct = None
    if phase == "checking":
        _mb_state["progress"] = "Checking for updates…"
    elif phase == "downloading":
        pct = update.get("pct")
        _mb_state["progress"] = (f"Downloading… {pct or 0}% "
                                 f"({update.get('downloaded_mb', 0)}/{update.get('total_mb', 0)} MB)")
    elif phase == "loading":
        pct = update.get("pct", 0)  # byte-weighted, smooth (loader-computed)
        _mb_state["progress"] = f"Loading database… {pct}%"
    elif phase == "analyzing":
        _mb_state["progress"] = "Analyzing…"
    elif phase == "done":
        pct = 100
        _mb_state["progress"] = "Up to date"
    if phase:
        _mb_state["phase"] = phase
    _mb_state["pct"] = pct
    notify_library_subscribers()


def _mb_worker(force: bool) -> None:
    try:
        import mb_dump_load
        # Re-checked here (not only in the endpoint) because maybe_auto_update
        # starts the worker directly — a full disk must fail before the
        # download, not 6 GB into it.
        budget = mb_dump_load.disk_budget()
        if not budget["can_fit"]:
            raise RuntimeError(f"insufficient disk: needs ~{budget['required_gb']} GB, "
                               f"{budget['free_gb']} GB free")
        result = mb_dump_load.download_and_load(_mb_progress, force=force)
        from datetime import datetime, timezone
        _write("musicbrainz.last_update_at", datetime.now(timezone.utc).isoformat())
        logger.info(f"MusicBrainz dump loaded: {result}")

        # The freshly-loaded dump makes every owned artist canon-pending
        # (last_mb_sync IS NULL) — canonicalize now so the EXISTING library gets
        # MBIDs + RG titles without waiting for a re-scan. Incremental on later
        # dump refreshes (only artists with new content since their last canon).
        _mb_state["phase"] = "canonicalizing"
        _mb_state["progress"] = "Canonicalizing library (MusicBrainz)…"
        _mb_state["pct"] = None
        notify_library_subscribers()
        # The dump tables exist only now — re-evaluate the data source so this
        # process (which may have started dump-less, LOCAL_DUMP=False) actually
        # canonicalizes instead of silently skipping. No backend restart needed.
        import mb_backend as mb
        mb.refresh()
        from canon.content import canonicalize_pending
        from canon import algo_canon
        canon = {}
        with algo_canon() as _ok:   # priority over AI; dump lock already released here
            if _ok:
                canon = canonicalize_pending()
                # A fresh dump may expose release-groups the matcher couldn't reach;
                # catch the NULL-rg owned-album residue (deluxe/multi-disc, fragments) by
                # name, then the content-only studio uniques (cross-script / reworded).
                from canon.content import distill_album_residue, distill_album_coverage
                canon["album_residue_bound"] = (distill_album_residue().get("bound", 0)
                                                + distill_album_coverage().get("bound", 0))
        logger.info(f"Post-load MB canon: {canon}")
        # A fresh dump is the moment to run the AI judgment tier once over the
        # whole residue (async, in the background — it's LLM-slow). Gated: no-op
        # unless the tier is enabled + a provider is authed.
        if start_aicanon_job():
            logger.info("Post-load: AI canonization started")
        _mb_progress({"phase": "done"})
    except Exception as e:
        _mb_state["error"] = str(e)
        _mb_state["progress"] = f"Failed: {e}"
        logger.error(f"MusicBrainz update failed: {e}", exc_info=True)
    finally:
        _mb_state["running"] = False
        notify_library_subscribers()


def _mb_section() -> Dict[str, Any]:
    """Stats + settings + live job state for the MusicBrainz block."""
    try:
        import mb_dump_load
        st = mb_dump_load.stats()
    except Exception as e:
        logger.warning(f"MB stats failed: {e}")
        st = {"loaded": False, "version": None, "total_records": 0, "size_bytes": 0}
    return {
        "loaded":          bool(st.get("loaded")),
        "version":         st.get("version"),
        "total_records":   st.get("total_records", 0),
        "size_bytes":      st.get("size_bytes", 0),
        "auto_update":     bool(_read("musicbrainz.auto_update")),
        "last_update_at":  _read("musicbrainz.last_update_at"),
        "update": {
            "running":  bool(_mb_state["running"]),
            "phase":    _mb_state["phase"],
            "progress": _mb_state["progress"],
            "pct":      _mb_state["pct"],
            "error":    _mb_state["error"],
        },
    }


def maybe_auto_update() -> None:
    """Called at backend startup. If the user enabled auto-update, check the
    mirror in a background thread and load a newer dump (or the first one).
    Non-blocking: the network check + ~7 GB download never gate startup."""
    if not bool(_read("musicbrainz.auto_update")):
        return

    def _check() -> None:
        try:
            import mb_dump_load
            latest = mb_dump_load.latest_version()
            if not latest:
                return
            current = mb_dump_load.loaded_version() == latest and mb_dump_load.stats().get("loaded")
            if current:
                return
            with _mb_lock:
                if _mb_state["running"]:
                    return
                _mb_state.update(running=True, phase="checking",
                                 progress="Auto-update…", pct=None, error=None)
            _mb_worker(False)
        except Exception as e:
            logger.warning(f"MusicBrainz auto-update check failed: {e}")

    threading.Thread(target=_check, daemon=True).start()


# ============================================================
# Reads
# ============================================================

async def _library_state() -> Dict[str, Any]:
    """Stats + ongoing scan progress for the Library section.

    Mirrors the launcher's `_update_stats_labels` shape so the web
    Library screen reads identical numbers to the launcher's stats
    panel — Tracks / Artists / Albums / Genres on the Library side,
    Embeddings / Features / Last.fm / Lyrics on the Enrichment side."""
    from main import get_stats, _scan_state, _enrich_state

    try:
        stats = await get_stats()
    except Exception as e:
        logger.warning(f"Library stats query failed: {e}")
        stats = {}

    scan = {
        "running":           bool(_scan_state.get("running")),
        "cancel_requested":  bool(_scan_state.get("cancel_requested")),
        "progress":          _scan_state.get("progress"),
        "stats":             _scan_state.get("stats"),
    }
    enrich = {
        "running":           bool(_enrich_state.get("running")),
        "cancel_requested":  bool(_enrich_state.get("cancel_requested")),
        "progress":          _enrich_state.get("progress"),
    }

    # Show the host-side music path (E:\Music etc.), not the
    # container's /music bind-mount. MUSIC_HOST_PATH is set by the
    # launcher when starting the container.
    music_path = app_settings.music_host_path or app_settings.music_library_path

    return {
        "music_path":         music_path,
        # Library counts (matches launcher's Library block 2×2)
        "total_tracks":       stats.get("total_tracks", 0),
        "total_artists":      stats.get("total_artists", 0),
        "total_albums":       stats.get("total_albums", 0),
        "total_genres":       stats.get("unique_genres", 0),
        "total_size_bytes":   stats.get("total_file_size_bytes", 0),
        # Enrichment coverage (matches launcher's Enrichment 2×2)
        "embeddings_done":    stats.get("tracks_with_embeddings", 0),
        "features_done":      stats.get("tracks_with_features", 0),
        "lyrics_done":        stats.get("tracks_with_lyrics", 0),
        "lastfm_done":        stats.get("artists_with_lastfm", 0),
        "lastfm_total":       stats.get("library_artists", 0),
        # Last scan + runtime workers
        "last_scan_at":       _read("library.last_scan_at"),
        "scan":               scan,
        "enrich":             enrich,
        "musicbrainz":        _mb_section(),
    }


def _ai_state() -> Dict[str, Any]:
    """Provider/model/auth/usage snapshot for the AI section.

    Authentication state is one of:
      - 'oauth_signed_in' — Claude Code OAuth is connected (existing
        flow). expires_in_days populated.
      - 'api_key_set'    — user has provided a raw API key.
        masked_key shows last 4 chars.
      - 'not_authenticated' — neither.

    Usage (balance, monthly spent, limit, days_left) is null when the
    provider doesn't expose a billing API for the current credential
    type — UI hides the row in that case."""
    provider = _read("ai.provider")
    model    = _read("ai.model")
    api_key  = _read("ai.api_key")

    auth_state = "not_authenticated"
    masked_key: Optional[str] = None
    expires_in_days: Optional[int] = None
    # Authentication state is only meaningful once a provider has been
    # picked. Without one we leave auth_state at the default so the UI
    # hides the auth/usage rows entirely.
    if provider:
        if api_key:
            auth_state = "api_key_set"
            masked_key = "●" * 8 + (api_key[-4:] if len(api_key) >= 4 else api_key)
        elif provider == "claude_code":
            # Claude Code is OAuth (subscription). claude_code.get_state() knows
            # where the credentials live — incl. /home/claudeuser/.claude in Docker,
            # where the CLI runs demoted to CLAUDE_USER. (Previously this imported a
            # non-existent routers.chat.oauth_status and swallowed the ImportError,
            # so auth_state was ALWAYS not_authenticated — which hid Claude Code on
            # Docker and disabled the AI-canon tier there.)
            try:
                import claude_code
                if claude_code.get_state() == "ready":
                    auth_state = "oauth_signed_in"
            except Exception:
                logger.exception("claude_code auth-state check failed")

    # Usage is intentionally null until we wire the provider billing
    # API; UI handles "row hidden when null". Placeholder structure
    # documents the shape we expect:
    usage = None  # {"spent": 3.20, "limit": 20, "days_left": 5}

    # AI canonization: default On only when the AI is "free" (subscription /
    # OAuth) — a paid API key bills per token, so default Off + the user opts in.
    canon_free = auth_state == "oauth_signed_in"
    canon_pref = _read("ai.canonization_enabled")
    canon_enabled = canon_free if canon_pref is None else bool(canon_pref)
    try:
        mb_dump = db_query_one("SELECT 1 FROM mb_artist LIMIT 1") is not None
    except Exception:
        mb_dump = False
    with _aicanon_lock:
        canon_job = dict(_aicanon)

    return {
        "provider":       provider,
        "model":          model,
        "auth_state":     auth_state,
        "masked_key":     masked_key,
        "expires_in_days": expires_in_days,
        "usage":          usage,
        "canonization": {
            "enabled":   canon_enabled,
            "is_default": canon_pref is None,
            "free":      canon_free,
            "available": bool(provider) and auth_state != "not_authenticated",
            "mb_dump":   mb_dump,   # the tier resolves against the local MB dump
            "job":       canon_job,
        },
    }


def _sync_state() -> Dict[str, Any]:
    from routers.p2p import ONLINE_WINDOW_MIN

    friends_online = 0
    friends_total = 0
    try:
        row = db_query_one(f"""
            SELECT
                COUNT(*) FILTER (WHERE is_blocked = FALSE) AS total,
                COUNT(*) FILTER (
                    WHERE is_blocked = FALSE
                      AND last_seen IS NOT NULL
                      AND last_seen > NOW()
                          - make_interval(mins => {ONLINE_WINDOW_MIN})
                ) AS online
            FROM friends
        """)
        if row:
            friends_total  = int(row.get("total") or 0)
            friends_online = int(row.get("online") or 0)
    except Exception as e:
        logger.warning(f"friends count failed: {e}")

    bg_status = None
    try:
        import background_enrichment
        bg_status = background_enrichment.status()
    except Exception as e:
        logger.warning(f"background_enrichment status read failed: {e}")

    def _relay_client_count() -> int:
        # Docker mode: the relay registry lives in this process. Launcher
        # mode: the count lives in the launcher's sync server, which this
        # backend cannot see — 0 is honest there ("none via this surface").
        try:
            from routers.peer_chat import relay_client_count
            return relay_client_count()
        except Exception:
            return 0

    return {
        "p2p_enabled":             bool(_read("sync.p2p_enabled")),
        "auto_interval_min":       _read("sync.auto_interval_min"),
        "announce_limit":          _read("sync.announce_limit"),
        "carry_limit":             _read("sync.carry_limit"),
        "background_enrichment":   bool(_read("enrichment.background_enabled")),
        "reanalyze_imported":      bool(_read("enrichment.reanalyze_imported")),
        "relay_enabled":           bool(_read("p2p.relay_enabled")),
        "relay_clients":           _relay_client_count(),
        "background_status":       bg_status,
        "last_sync_at":            _read("sync.last_at"),
        "last_items_received":     _read("sync.last_items_received"),
        "friends_online":          friends_online,
        "friends_total":           friends_total,
        "reachability":            _read("p2p.reachability") or "unknown",
        "reachability_detail":     _read("p2p.reachability_detail"),
        "reachability_checked_at": _read("p2p.reachability_checked_at"),
    }


def _audio_output_state() -> Dict[str, Any]:
    """Lightweight Audio output stub.

    We deliberately do NOT call routers.hqplayer.get_state() here —
    that endpoint does a full multi-roundtrip status/info/modes/rates
    fetch and can block for seconds when HQPlayer is unreachable. The
    frontend pings /api/hqplayer/state separately (with its own
    timeout / re-render) for the live connection indicator. Settings
    just returns the configured host:port so the row paints
    instantly with `connected: null`."""
    return {
        "hqplayer_connected": None,
        "hqplayer_host":      app_settings.hqplayer_host,
        "hqplayer_port":      app_settings.hqplayer_port,
    }


@router.get("/library")
async def get_library_state() -> Dict[str, Any]:
    return await _library_state()


@router.get("/library/stream")
async def library_stream() -> StreamingResponse:
    """SSE channel. Emits a wake event whenever scan / enrich workers
    transition state. The client receives "data: {}" and pulls fresh
    state via GET /api/settings/library. Replaces 1.5s polling.

    Pattern mirrors /api/p2p/chat/stream — wake-event, not payload."""
    loop = asyncio.get_event_loop()
    evt = asyncio.Event()

    async def event_generator():
        try:
            with _library_sse_lock:
                _library_sse_clients.append((evt, loop))

            # Initial ping so the client knows the channel is live.
            yield "data: {}\n\n"

            while True:
                try:
                    await asyncio.wait_for(evt.wait(), timeout=20.0)
                    evt.clear()
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
                    continue
                yield "data: {}\n\n"
        except asyncio.CancelledError:
            pass
        finally:
            with _library_sse_lock:
                _library_sse_clients[:] = [
                    (e, l) for e, l in _library_sse_clients if e is not evt
                ]

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/ai")
def get_ai_state() -> Dict[str, Any]:
    return _ai_state()


@router.get("/sync")
def get_sync_state() -> Dict[str, Any]:
    return _sync_state()


@router.get("")
async def get_settings() -> Dict[str, Any]:
    """Aggregate roundtrip (kept for clients that want one fetch)."""
    return {
        "library":      await _library_state(),
        "ai":           _ai_state(),
        "sync":         _sync_state(),
        "audio_output": _audio_output_state(),
    }


# ============================================================
# AI preferences
# ============================================================

class AiProviderUpdate(BaseModel):
    provider: str = Field(..., max_length=40)


class AiModelUpdate(BaseModel):
    model: str = Field(..., max_length=80)


class AiKeyUpdate(BaseModel):
    api_key: Optional[str] = Field(default=None, max_length=300)


def _canon_provider(p: Optional[str]) -> Optional[str]:
    """Web UI used to expose the Anthropic API provider as 'claude';
    backend registers it as 'anthropic'. Mapping at this layer keeps
    per-provider key/model buckets coherent across the rename."""
    return "anthropic" if p == "claude" else p


@router.put("/ai/provider")
def put_ai_provider(req: AiProviderUpdate) -> Dict[str, Any]:
    """Switch provider. Stash the current model/key under per-provider
    keys so the next switch back restores them, then activate the new
    provider's saved model/key (or clear when there's nothing saved).
    The single `ai.api_key` / `ai.model` rows stay the source of truth
    for `_ai_state()` and `_resolve_*()` — switching just swaps what
    lives in them."""
    cur_provider = _canon_provider(_read("ai.provider"))
    new_provider = _canon_provider(req.provider) or req.provider

    # Stash the current pair under the *outgoing* provider's bucket
    # before overwriting the active slot.
    if cur_provider and cur_provider != new_provider:
        _write(f"ai.{cur_provider}.api_key", _read("ai.api_key"))
        _write(f"ai.{cur_provider}.model",   _read("ai.model"))

    # Restore the *incoming* provider's pair, or clear if unset.
    next_key   = _read(f"ai.{new_provider}.api_key")
    next_model = _read(f"ai.{new_provider}.model")
    _write("ai.api_key", next_key)
    _write("ai.key",     next_key)  # legacy alias
    _write("ai.model",   next_model)
    _write("ai.provider", new_provider)

    # Re-overlay credentials onto Pydantic settings and bust the
    # providers cache — same path PUT /ai/key uses.
    _apply_api_key_to_runtime(next_key)

    # Connecting the AI after a dump was already loaded is the third trigger
    # (besides dump-load + scan): catch up the residue now. Gated + no-op without
    # the dump / when disabled, async in the background.
    start_aicanon_job()

    return {"provider": new_provider}


@router.put("/ai/model")
def put_ai_model(req: AiModelUpdate) -> Dict[str, Any]:
    _write("ai.model", req.model)
    provider = _canon_provider(_read("ai.provider"))
    if provider:
        _write(f"ai.{provider}.model", req.model)
    return {"model": req.model}


def _apply_api_key_to_runtime(key: Optional[str]) -> None:
    """Push the saved key onto Settings so providers/__init__ creates
    the right provider on the next access. Mirrors the lastfm overlay
    in main.py — Pydantic Settings only reads .env at startup, so
    Web-UI key edits would otherwise stay invisible to the provider
    registry. Key is attributed by the currently-selected provider —
    the UI only exposes one key slot at a time, so we know whose
    credential this is."""
    from config import settings as app_settings
    provider = _read("ai.provider")
    if provider == "anthropic":
        app_settings.anthropic_api_key = key
    elif provider == "openai":
        app_settings.openai_api_key = key
    # Tear down the cached registry so the next get_provider() call
    # rebuilds with the new credential.
    try:
        from providers import reset as _reset_providers
        _reset_providers()
    except Exception:
        pass


@router.put("/ai/key")
def put_ai_key(req: AiKeyUpdate) -> Dict[str, Any]:
    """Set or clear the user-supplied API key. Validation against the
    provider's API is deferred — the UI's chat surface will surface
    auth errors on first call. Empty/null clears the key. Also stored
    under `ai.<provider>.api_key` so the next provider switch preserves
    it for restoration."""
    key = (req.api_key or "").strip() or None
    _write("ai.key", key)  # legacy alias readable elsewhere
    _write("ai.api_key", key)
    provider = _canon_provider(_read("ai.provider"))
    if provider:
        _write(f"ai.{provider}.api_key", key)
    _apply_api_key_to_runtime(key)
    return _ai_state()


@router.delete("/ai/key")
def delete_ai_key() -> Dict[str, Any]:
    _write("ai.api_key", None)
    _write("ai.key", None)
    provider = _canon_provider(_read("ai.provider"))
    if provider:
        _write(f"ai.{provider}.api_key", None)
    _apply_api_key_to_runtime(None)
    return _ai_state()


class AiCanonUpdate(BaseModel):
    enabled: bool


@router.put("/ai/canonization")
def put_ai_canonization(req: AiCanonUpdate) -> Dict[str, Any]:
    """Toggle the AI canonization tier (explicit user choice overrides the
    free/paid default)."""
    _write("ai.canonization_enabled", bool(req.enabled))
    return _ai_state()


@router.post("/ai/canonization/run")
def run_ai_canonization() -> Dict[str, Any]:
    """Manual single-flight AI canonization run over the full residue. Returns
    immediately; progress streams over /ai/canonization/stream + GET /ai. force=
    True so the manual button works even if the auto-trigger gate is off."""
    started = start_aicanon_job(force=True)
    return {"started": started, "running": not started}


@router.get("/ai/canonization/stream")
async def ai_canonization_stream() -> StreamingResponse:
    """SSE wake channel for the AI canonization run — client re-pulls GET /ai on
    each event. Same wake-event pattern as the library/claude channels."""
    loop = asyncio.get_event_loop()
    evt = asyncio.Event()

    async def event_generator():
        try:
            with _aicanon_sse_lock:
                _aicanon_sse_clients.append((evt, loop))
            yield "data: {}\n\n"
            while True:
                try:
                    await asyncio.wait_for(evt.wait(), timeout=20.0)
                    evt.clear()
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
                    continue
                yield "data: {}\n\n"
        except asyncio.CancelledError:
            pass
        finally:
            with _aicanon_sse_lock:
                _aicanon_sse_clients[:] = [
                    (e, l) for e, l in _aicanon_sse_clients if e is not evt
                ]

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive",
                 "X-Accel-Buffering": "no"},
    )


def load_ai_credentials_from_db() -> None:
    """Startup-time overlay: bring DB-stored credentials onto Settings
    so the first request after a backend restart already sees them.
    Called from main.lifespan."""
    try:
        provider = _read("ai.provider")
        key = _read("ai.api_key")
        if not key:
            return
        from config import settings as app_settings
        if provider == "anthropic":
            app_settings.anthropic_api_key = key
        elif provider == "openai":
            app_settings.openai_api_key = key
    except Exception as e:
        logger.warning(f"Failed to load AI credentials from DB: {e}")


# ============================================================
# Claude Code (subscription CLI) state + install + sign-in
# ============================================================

# Reuse the existing _scan/_enrich pattern: a single dict tracks the
# install job so the UI can poll it while npm runs in a thread.
_claude_install_state: Dict[str, Any] = {
    "running": False,
    "progress": "",
    "error": None,
}


@router.get("/ai/claude/stream")
async def claude_stream() -> StreamingResponse:
    """SSE channel for the AI > Claude Code screen. Wakes whenever the
    install worker mutates `_claude_install_state` or the signin
    endpoint is invoked, so the client re-fetches /api/settings/ai/claude/state
    in response to a real event instead of polling on a 2s timer.

    Same wake-event pattern as /api/settings/library/stream — server
    emits "data: {}" and the client re-pulls state."""
    loop = asyncio.get_event_loop()
    evt = asyncio.Event()

    async def event_generator():
        try:
            with _claude_sse_lock:
                _claude_sse_clients.append((evt, loop))
            yield "data: {}\n\n"
            while True:
                try:
                    await asyncio.wait_for(evt.wait(), timeout=20.0)
                    evt.clear()
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
                    continue
                yield "data: {}\n\n"
        except asyncio.CancelledError:
            pass
        finally:
            with _claude_sse_lock:
                _claude_sse_clients[:] = [
                    (e, l) for e, l in _claude_sse_clients if e is not evt
                ]

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/ai/claude/state")
def get_claude_state() -> Dict[str, Any]:
    """State machine for the Claude Code subscription CLI. The Web UI
    branches on `state` to show install / sign-in / ready affordances.
    `host_unsupported` means the backend can't shell out (Docker mode)
    and the UI should point the user at the Desktop Launcher.

    When the state transitions to 'ready' we invalidate the providers
    cache so /api/chat picks up the freshly-installed CLI without a
    backend restart."""
    from claude_code import (
        get_state, get_claude_executable, detect_node_version,
        is_launcher_mode,
    )
    state = get_state()
    if state == "ready":
        from providers import reset as _reset_providers
        _reset_providers()
    node_ver = detect_node_version()
    claude = get_claude_executable()
    return {
        "state":          state,
        "launcher_mode":  is_launcher_mode(),
        "node_version":   ".".join(str(p) for p in node_ver) if node_ver else None,
        "claude_path":    str(claude) if claude else None,
        "install":        dict(_claude_install_state),
    }


@router.post("/ai/claude/install")
def post_claude_install() -> Dict[str, Any]:
    """Kick off `npm install @anthropic-ai/claude-code` in a worker
    thread. Returns immediately; the client polls /state until the
    job's `install.running` goes false. Idempotent — calling while a
    job is already running returns the in-flight state."""
    import threading
    from claude_code import (
        is_launcher_mode, detect_node_version, install_claude_runtime,
    )
    if not is_launcher_mode():
        raise HTTPException(
            status_code=400,
            detail="Claude Code install is only available in the native launcher. "
                   "Open Sautium's Desktop Launcher to install.",
        )
    if _claude_install_state.get("running"):
        return dict(_claude_install_state)

    node_ver = detect_node_version()
    if node_ver is None or node_ver[0] < 18:
        raise HTTPException(
            status_code=400,
            detail="Node.js 18+ is required. Re-run the Sautium installer to repair the Node bundle.",
        )

    _claude_install_state.update({
        "running": True,
        "progress": "Running npm install…",
        "error":   None,
    })
    notify_claude_subscribers()

    def _worker():
        try:
            ok, msg = install_claude_runtime()
            _claude_install_state["progress"] = msg
            _claude_install_state["error"] = None if ok else msg
        except Exception as e:
            _claude_install_state["error"] = str(e)
        finally:
            _claude_install_state["running"] = False
            notify_claude_subscribers()

    threading.Thread(target=_worker, daemon=True, name="claude-install").start()
    return dict(_claude_install_state)


@router.post("/ai/claude/signin")
def post_claude_signin() -> Dict[str, Any]:
    """Launch a terminal window running the `claude` CLI so the user
    can run `/login`. Caller polls /state.state for transition to
    'ready'. Returns immediately."""
    from claude_code import is_launcher_mode, launch_signin_terminal
    if not is_launcher_mode():
        raise HTTPException(
            status_code=400,
            detail="Sign-in requires a native terminal — use the Desktop Launcher.",
        )
    try:
        launch_signin_terminal()
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    return {"opened": True}


# ============================================================
# Albums sort (Artist screen)
# ============================================================

_ALBUMS_SORTS = {
    "release_year", "time_listened", "popularity",
    "recently_added", "a_z",
}


class AlbumsSortUpdate(BaseModel):
    sort: str = Field(..., max_length=40)


@router.get("/albums-sort")
def get_albums_sort() -> Dict[str, Any]:
    val = _read("albums.sort")
    if val not in _ALBUMS_SORTS:
        val = "release_year"
    return {"sort": val}


@router.put("/albums-sort")
def put_albums_sort(req: AlbumsSortUpdate) -> Dict[str, Any]:
    if req.sort not in _ALBUMS_SORTS:
        raise HTTPException(status_code=400, detail="Unknown sort key")
    _write("albums.sort", req.sort)
    return {"sort": req.sort}


# ============================================================
# Hardware profile (full / standard / lite — HARDWARE-TIERS.md)
# ============================================================

@router.get("/hardware")
def get_hardware_profile() -> Dict[str, Any]:
    """Read-only: selection is automatic (SAUTIUM_PROFILE env is the only
    override, for diagnostics). Feeds the info block on the Library screen."""
    import hardware_profile
    return hardware_profile.resolve().describe()


# ============================================================
# HQPlayer connection
# ============================================================

class HqplayerPrefs(BaseModel):
    host: Optional[str] = Field(default=None, max_length=255)
    port: Optional[int] = Field(default=None, ge=1, le=65535)


@router.get("/hqplayer")
def get_hqplayer_prefs() -> Dict[str, Any]:
    from config import settings as app_settings
    return {
        "host": _read("hqplayer.host") or app_settings.hqplayer_host,
        "port": _read("hqplayer.port") or app_settings.hqplayer_port,
    }


@router.put("/hqplayer")
def put_hqplayer_prefs(req: HqplayerPrefs) -> Dict[str, Any]:
    """Update HQPlayer host/port. Persisted to user_settings and
    overlaid onto Pydantic settings runtime so the control client —
    and the status poller that play tracking now rides on — reconnect
    to the new endpoint on the next call, no restart needed."""
    from config import settings as app_settings
    if req.host is not None:
        host = req.host.strip() or None
        _write("hqplayer.host", host)
        if host:
            app_settings.hqplayer_host = host
    if req.port is not None:
        _write("hqplayer.port", req.port)
        app_settings.hqplayer_port = req.port
    logger.info(
        f"HQPlayer settings updated: host={app_settings.hqplayer_host}, "
        f"port={app_settings.hqplayer_port}"
    )
    # Drop cached HQPlayer sockets so the next status/command call
    # reconnects against the new address.
    try:
        from playback.hqp_backend import reset_all_clients as _reset_hqp
        _reset_hqp()
        logger.info("HQPlayer cached clients reset")
    except Exception as e:
        logger.warning(f"HQPlayer client reset failed: {e}")
    # The HQPlayer backend is gated on a configured endpoint — saving a host
    # is the event that activates it (idempotent when already running);
    # clearing the host detaches it on HQP-less nodes. When another output
    # type is explicitly selected, saving an HQPlayer address must NOT
    # hijack the active output.
    try:
        from routers.player import (start_status_poller, stop_status_poller,
                                    _hqp_configured)
        # Only when HQPlayer IS the selection. Unset no longer means "HQPlayer
        # may have it" — an unset output resolves to this device, so saving an
        # address here must not quietly move playback.
        if _read("output.type") == "hqplayer":
            if _hqp_configured():
                start_status_poller()
            else:
                stop_status_poller()
    except Exception as e:
        logger.warning(f"Status poller toggle failed: {e}")
    return get_hqplayer_prefs()


class OutputPrefs(BaseModel):
    type: Optional[str] = None          # 'hqplayer' | 'local' | 'dlna'
    device_id: Optional[str] = None     # local: "{hostapi}::{name}"
    exclusive: Optional[bool] = None    # local: WASAPI exclusive mode
    renderer: Optional[Dict[str, Any]] = None   # dlna: {udn, location, name}
    stream_quality: Optional[str] = None    # lossless | opus_192 | opus_96


@router.get("/output")
def get_output_prefs() -> Dict[str, Any]:
    return {
        "type": _read("output.type"),
        "device_id": _read("output.local_device"),
        "exclusive": bool(_read("output.local_exclusive")),
        "renderer": _read("output.dlna_renderer"),
        "stream_quality": _read("output.stream_quality") or "lossless",
    }


@router.put("/output")
def put_output_prefs(req: OutputPrefs) -> Dict[str, Any]:
    """Select the playback output (Output picker). Persisted, then applied
    live: the old backend stops, the canonical queue survives, playback does
    not auto-resume — the user presses play on the new output."""
    from playback.manager import manager

    if req.type is not None and req.type not in ("hqplayer", "local", "dlna", "browser"):
        raise HTTPException(status_code=400, detail=f"unknown output type: {req.type}")
    if req.type == "local":
        from playback.local import devices as local_devices
        if not local_devices.list_devices():
            raise HTTPException(
                status_code=409,
                detail="No local audio devices — the backend must run natively "
                       "(launcher mode) for WASAPI/ASIO/CoreAudio output")
    if req.type == "dlna" and not (req.renderer or _read("output.dlna_renderer")):
        raise HTTPException(status_code=409, detail="No DLNA renderer selected")
    if req.stream_quality is not None:
        if req.stream_quality not in ("lossless", "opus_192", "opus_96"):
            raise HTTPException(status_code=400,
                                detail=f"unknown stream quality: {req.stream_quality}")
        _write("output.stream_quality", req.stream_quality)
    if req.type is not None:
        _write("output.type", req.type)
    if req.device_id is not None:
        _write("output.local_device", req.device_id or None)
    if req.exclusive is not None:
        _write("output.local_exclusive", bool(req.exclusive))
    if req.renderer is not None:
        _write("output.dlna_renderer", req.renderer or None)

    # A quality-only change must NOT re-activate the output (that would cut
    # the current track). It just alters the next track's media URL —
    # exactly the "current track plays out, change applies from the next
    # one" behavior. Re-activate only when the output identity changed.
    if not (req.type is not None or req.device_id is not None
            or req.exclusive is not None or req.renderer is not None):
        return get_output_prefs()

    otype = _read("output.type")
    try:
        if otype == "browser":
            manager.activate(None)
            manager.activate("browser")
        elif otype == "dlna":
            manager.activate(None)
            manager.activate("dlna", renderer=_read("output.dlna_renderer"))
        elif otype == "local":
            # Deactivate first — re-selecting "local" with a different device
            # must reconstruct the engine, and activate() is idempotent per type.
            manager.activate(None)
            manager.activate("local",
                             device_id=_read("output.local_device"),
                             exclusive=bool(_read("output.local_exclusive")))
        elif otype == "hqplayer":
            # Choosing HQPlayer IS configuring it. There is always an effective
            # endpoint (the config default, localhost:4321), so persist it if
            # nothing was saved and carry on.
            #
            # This used to refuse with a 409 when no host had been saved, using
            # the same predicate that decides whether to POLL HQPlayer. Those
            # are different questions, and conflating them left the setting
            # unreachable: nothing configures HQPlayer during setup any more,
            # so a new node could not select it, and the screen where a host is
            # entered is only listed while HQPlayer is the active output.
            if not _read("hqplayer.host"):
                _write("hqplayer.host", app_settings.hqplayer_host)
                _write("hqplayer.port", app_settings.hqplayer_port)
                logger.info("HQPlayer selected with no saved host — recorded "
                            "the default %s:%s", app_settings.hqplayer_host,
                            app_settings.hqplayer_port)
            manager.activate(None)
            manager.activate("hqplayer")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=503, detail=str(e))
    return get_output_prefs()


def load_hqplayer_from_db() -> None:
    """Startup overlay: copy persisted host/port onto settings before
    the first request."""
    try:
        from config import settings as app_settings
        host = _read("hqplayer.host")
        port = _read("hqplayer.port")
        if host:
            app_settings.hqplayer_host = host
        if port:
            app_settings.hqplayer_port = int(port)
    except Exception as e:
        logger.warning(f"Failed to load HQPlayer settings from DB: {e}")


# ============================================================
# Sync & P2P preferences
# ============================================================

class SyncPrefs(BaseModel):
    p2p_enabled:             Optional[bool] = None
    auto_interval_min:       Optional[int]  = None  # null disables
    announce_limit:          Optional[int]  = None  # rare-artist tail size
    carry_limit:             Optional[int]  = None  # foreign tracks carried
    background_enrichment:   Optional[bool] = None
    reanalyze_imported:      Optional[bool] = None
    relay_enabled:           Optional[bool] = None


@router.put("/sync")
def put_sync_prefs(req: SyncPrefs) -> Dict[str, Any]:
    if req.p2p_enabled is not None:
        _write("sync.p2p_enabled", bool(req.p2p_enabled))
    if req.auto_interval_min is not None:
        # None gets coerced to null serverside via _write — explicit
        # 0 here means "every 0 min" (disabled in the UI). We store
        # the raw int; UI maps 0/null → "Disabled".
        _write("sync.auto_interval_min",
               int(req.auto_interval_min) if req.auto_interval_min > 0 else None)
    if req.announce_limit is not None:
        _write("sync.announce_limit",
               int(req.announce_limit) if req.announce_limit > 0 else None)
    if req.carry_limit is not None:
        _write("sync.carry_limit",
               int(req.carry_limit) if req.carry_limit > 0 else None)
    if req.reanalyze_imported is not None:
        _write("enrichment.reanalyze_imported", bool(req.reanalyze_imported))
    if req.relay_enabled is not None:
        _write("p2p.relay_enabled", bool(req.relay_enabled))
    if req.background_enrichment is not None:
        want = bool(req.background_enrichment)
        _write("enrichment.background_enabled", want)
        # Drive the loop directly off the flag — the toggle is the
        # source of truth, so the persisted value and the running
        # thread can never disagree.
        try:
            import background_enrichment
            if want:
                background_enrichment.start()
            else:
                background_enrichment.stop()
        except Exception as e:
            logger.warning(f"background_enrichment toggle failed: {e}")
    return _sync_state()


class MbPrefs(BaseModel):
    auto_update: Optional[bool] = None


@router.put("/musicbrainz")
def put_mb_prefs(req: MbPrefs) -> Dict[str, Any]:
    if req.auto_update is not None:
        _write("musicbrainz.auto_update", bool(req.auto_update))
    return _mb_section()


@router.post("/musicbrainz/delete")
def mb_delete() -> Dict[str, Any]:
    """Remove the local MusicBrainz dump. The wizard pre-ticks the download
    when the disk allows it; this is the other half of that bargain."""
    with _mb_lock:
        if _mb_state["running"]:
            raise HTTPException(status_code=409,
                                detail="MusicBrainz update running")
    try:
        import mb_dump_load
        result = mb_dump_load.delete_dump()
    except RuntimeError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except Exception as e:
        logger.error(f"MB dump delete failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))
    notify_library_subscribers()
    return {"success": True, **result}


@router.get("/musicbrainz/status")
def mb_status() -> Dict[str, Any]:
    """Dump state + disk budget in one read — what the DJ agent's
    mb_dump_status tool quotes before offering (or declining) a download."""
    import mb_dump_load
    return {**_mb_section(), "disk": mb_dump_load.disk_budget()}


@router.post("/musicbrainz/update")
def mb_update(force: bool = False) -> Dict[str, Any]:
    """Download (if newer) + stream-load the MusicBrainz dump in the background."""
    import mb_dump_load
    budget = mb_dump_load.disk_budget()
    if not budget["can_fit"]:
        # Synchronous refusal (the DJ agent must not report "started"), and
        # surfaced in _mb_state so the Settings screen's fire-and-forget POST
        # shows the reason instead of silently doing nothing.
        msg = (f"insufficient disk: needs ~{budget['required_gb']} GB, "
               f"{budget['free_gb']} GB free")
        with _mb_lock:
            if not _mb_state["running"]:
                _mb_state.update(phase="error", progress=f"Failed: {msg}", error=msg)
        notify_library_subscribers()
        raise HTTPException(status_code=507, detail=msg)
    with _mb_lock:
        if _mb_state["running"]:
            raise HTTPException(status_code=409, detail="MusicBrainz update already running")
        _mb_state.update(running=True, phase="checking",
                         progress="Starting…", pct=None, error=None)
    threading.Thread(target=_mb_worker, args=(bool(force),), daemon=True).start()
    notify_library_subscribers()
    return {"success": True}


@router.post("/sync/force")
def force_sync() -> Dict[str, Any]:
    """Kick the P2P sync runner manually.

    Fires NOTIFY sautium_sync_request — the launcher's P2PManager
    listens on this channel and runs sync_from_peers() in its asyncio
    loop. The sync is asynchronous; the listener writes sync.last_at
    and sync.last_items_received on completion and emits
    NOTIFY sautium_sync_done so the SSE stream can push the result."""
    db_execute("NOTIFY sautium_sync_request")
    return {"ok": True, "triggered": True}


# ============================================================
# Library actions (thin passthroughs for the Settings buttons)
# ============================================================

@router.post("/library/scan")
async def trigger_scan(prune: bool = False) -> Dict[str, Any]:
    """Start a library scan from the Settings screen.

    Delegates to the existing /scan/start endpoint logic so we don't
    duplicate the scan-worker setup. `prune=true` also removes DB
    rows whose underlying files have disappeared from the music
    folder (slower; the default is add-only)."""
    from main import scan_start
    return await scan_start(prune=prune)


@router.post("/library/scan/cancel")
async def cancel_scan() -> Dict[str, Any]:
    from main import scan_cancel
    return await scan_cancel()


@router.post("/library/enrich")
async def trigger_enrich() -> Dict[str, Any]:
    """Start enrichment for items missing bios / audio analysis."""
    from main import enrich_start
    # default args; the existing endpoint sets sensible options
    return await enrich_start()  # type: ignore[call-arg]


@router.post("/library/enrich/cancel")
async def cancel_enrich() -> Dict[str, Any]:
    from main import enrich_cancel
    return await enrich_cancel()
