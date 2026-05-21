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
import threading
from typing import Any, Dict, List, Optional, Tuple

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


# ============================================================
# Preference keys + defaults (single source of truth)
# ============================================================

# user_settings JSONB stores one row per key, value can be any JSON.
# Defaults are returned to the UI when the row is missing so first-
# run users get a sensible Settings screen with no prior writes.
_DEFAULTS: Dict[str, Any] = {
    "sync.p2p_enabled":          True,
    "sync.auto_interval_min":    30,
    "sync.announce_limit":       None,   # null = announce all
    "sync.announce_rotation_min": 30,
    "enrichment.background_enabled": True,
    # Provider/model default to None so the first-run UI shows
    # "Not selected" instead of pretending Claude is picked when the
    # wizard offered an explicit "no AI" option.
    "ai.provider":               None,
    "ai.model":                  None,
    "ai.api_key":                None,
    # last sync metadata — written by the sync runner when a cycle
    # completes; surfaced in the "Last sync · N new items" row.
    "sync.last_at":              None,
    "sync.last_items_received":  None,
}


def _read(key: str) -> Any:
    row = db_query_one("SELECT value FROM user_settings WHERE key = %(k)s", {"k": key})
    if row is None:
        return _DEFAULTS.get(key)
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
        "lastfm_done":        (stats.get("artists_with_lastfm", 0)
                               + stats.get("albums_with_lastfm", 0)),
        "lastfm_total":       (stats.get("library_artists", 0)
                               + stats.get("library_albums", 0)),
        # Last scan + runtime workers
        "last_scan_at":       _read("library.last_scan_at"),
        "scan":               scan,
        "enrich":             enrich,
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
        else:
            # OAuth check — chat.py persists a refresh token; if present
            # we treat the user as OAuth-signed-in. Days-to-expiry is
            # best-effort: parse the JWT or fall back to a placeholder.
            try:
                from routers.chat import oauth_status  # type: ignore
                st = oauth_status()
                if st and st.get("authenticated"):
                    auth_state = "oauth_signed_in"
                    expires_in_days = st.get("expires_in_days")
            except Exception:
                pass

    # Usage is intentionally null until we wire the provider billing
    # API; UI handles "row hidden when null". Placeholder structure
    # documents the shape we expect:
    usage = None  # {"spent": 3.20, "limit": 20, "days_left": 5}

    return {
        "provider":       provider,
        "model":          model,
        "auth_state":     auth_state,
        "masked_key":     masked_key,
        "expires_in_days": expires_in_days,
        "usage":          usage,
    }


def _sync_state() -> Dict[str, Any]:
    friends_online = 0
    friends_total = 0
    try:
        row = db_query_one("""
            SELECT
                COUNT(*) FILTER (WHERE is_blocked = FALSE) AS total,
                COUNT(*) FILTER (
                    WHERE is_blocked = FALSE
                      AND last_seen IS NOT NULL
                      AND last_seen > NOW() - INTERVAL '5 minutes'
                ) AS online
            FROM friends
        """)
        if row:
            friends_total  = int(row.get("total") or 0)
            friends_online = int(row.get("online") or 0)
    except Exception as e:
        logger.warning(f"friends count failed: {e}")

    return {
        "p2p_enabled":             bool(_read("sync.p2p_enabled")),
        "auto_interval_min":       _read("sync.auto_interval_min"),
        "announce_limit":          _read("sync.announce_limit"),
        "announce_rotation_min":   _read("sync.announce_rotation_min"),
        "background_enrichment":   bool(_read("enrichment.background_enabled")),
        "last_sync_at":            _read("sync.last_at"),
        "last_items_received":     _read("sync.last_items_received"),
        "friends_online":          friends_online,
        "friends_total":           friends_total,
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


@router.put("/ai/provider")
def put_ai_provider(req: AiProviderUpdate) -> Dict[str, Any]:
    _write("ai.provider", req.provider)
    return {"provider": req.provider}


@router.put("/ai/model")
def put_ai_model(req: AiModelUpdate) -> Dict[str, Any]:
    _write("ai.model", req.model)
    return {"model": req.model}


@router.put("/ai/key")
def put_ai_key(req: AiKeyUpdate) -> Dict[str, Any]:
    """Set or clear the user-supplied API key. Validation against the
    provider's API is deferred — the UI's chat surface will surface
    auth errors on first call. Empty/null clears the key."""
    key = (req.api_key or "").strip() or None
    _write("ai.key", key)  # legacy alias readable elsewhere
    _write("ai.api_key", key)
    return _ai_state()


@router.delete("/ai/key")
def delete_ai_key() -> Dict[str, Any]:
    _write("ai.api_key", None)
    _write("ai.key", None)
    return _ai_state()


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

    def _worker():
        try:
            ok, msg = install_claude_runtime()
            _claude_install_state["progress"] = msg
            _claude_install_state["error"] = None if ok else msg
        except Exception as e:
            _claude_install_state["error"] = str(e)
        finally:
            _claude_install_state["running"] = False

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
# Sync & P2P preferences
# ============================================================

class SyncPrefs(BaseModel):
    p2p_enabled:             Optional[bool] = None
    auto_interval_min:       Optional[int]  = None  # null disables
    announce_limit:          Optional[int]  = None  # null = announce all
    announce_rotation_min:   Optional[int]  = None
    background_enrichment:   Optional[bool] = None


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
    if req.announce_rotation_min is not None:
        _write("sync.announce_rotation_min", int(req.announce_rotation_min))
    if req.background_enrichment is not None:
        _write("enrichment.background_enabled", bool(req.background_enrichment))
    return _sync_state()


@router.post("/sync/force")
def force_sync() -> Dict[str, Any]:
    """Kick the P2P sync runner manually. Implementation is a stub
    for now — the desktop launcher owns the actual sync loop. We
    write a placeholder 'last sync' marker so the UI's transient
    toast reflects the action. Wire the real call when the launcher
    grows a backend-callable trigger."""
    from datetime import datetime, timezone
    _write("sync.last_at", datetime.now(timezone.utc).isoformat())
    _write("sync.last_items_received", 0)
    return {"ok": True, "items_received": 0, "note": "stub — wire to launcher sync trigger"}


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
