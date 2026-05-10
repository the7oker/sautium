"""
HQPlayer settings endpoint.

Single round-trip /state for the More → HQPlayer screen: connection
status, current DSP selections, all available lists (modes / rates /
filters / shapers / matrix profiles), and the user's favourite-filter
list from the user_settings key/value store. Writes go through
/config (any subset of filter / mode / rate / shaper / matrix_profile)
and /favorites (add/remove a filter name from the favourites list).

The screen reads state on mount only — no polling. If the user
changes settings via HQP Desktop, a manual refresh in the UI picks
up the new values.
"""

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from config import settings
from db_pool import db_execute, db_query_one
from routers.player import (
    _hqp_lock,
    _hqp_status_lock,
    _get_hqp,
    _get_hqp_status,
    _reset_hqp,
    _reset_hqp_status,
)

router = APIRouter(prefix="/api/hqplayer", tags=["hqplayer"])

_FAVORITES_KEY = "hqplayer.favorite_filters"


def _load_favorites() -> List[str]:
    row = db_query_one(
        "SELECT value FROM user_settings WHERE key = %(k)s",
        {"k": _FAVORITES_KEY},
    )
    if not row or not isinstance(row.get("value"), list):
        return []
    return [str(x) for x in row["value"] if isinstance(x, str)]


def _save_favorites(favs: List[str]) -> None:
    import json
    db_execute(
        """
        INSERT INTO user_settings (key, value) VALUES (%s, %s::jsonb)
        ON CONFLICT (key) DO UPDATE
            SET value = EXCLUDED.value,
                updated_at = CURRENT_TIMESTAMP
        """,
        (_FAVORITES_KEY, json.dumps(favs)),
    )


# -- Request models -----------------------------------------------------------

class ConfigRequest(BaseModel):
    filter: Optional[int] = None        # filter index from get_filters()
    filter1x: Optional[int] = None      # PCM 1x filter; paired with `filter`
    shaper: Optional[int] = None
    mode: Optional[int] = None
    rate: Optional[int] = None
    matrix_profile: Optional[str] = None


class FavoriteRequest(BaseModel):
    name: str
    action: str                         # 'add' | 'remove'


class VolumeRequest(BaseModel):
    # +1 nudges up by 1 dB; -1 nudges down. Bigger steps are
    # accepted so the same endpoint can serve a larger button or a
    # slider drag. HQPlayer's own volume_up/down step is 0.5 dB,
    # which is too granular for tap-tap-tap; we apply set_volume
    # with the delta computed from the current state.
    delta: float


# -- State --------------------------------------------------------------------

@router.get("/state")
def get_state() -> Dict[str, Any]:
    """Snapshot everything the HQPlayer settings screen needs."""
    response: Dict[str, Any] = {
        "host": settings.hqplayer_host,
        "port": settings.hqplayer_port,
        "connected": False,
    }

    try:
        with _hqp_status_lock:
            try:
                hqp = _get_hqp_status()
            except (BrokenPipeError, ConnectionError, OSError):
                _reset_hqp_status()
                hqp = _get_hqp_status()
            info = hqp.get_info()
            state = hqp.get_state()
            modes = hqp.get_modes()
            rates = hqp.get_rates()
            filters = hqp.get_filters()
            shapers = hqp.get_shapers()
            try:
                matrix_profiles = hqp.matrix_list_profiles()
            except Exception:
                matrix_profiles = []
    except (BrokenPipeError, ConnectionError, OSError) as e:
        return {**response, "error": str(e)}

    response["connected"] = True
    response["info"] = info or {}
    response["state"] = state or {}
    response["modes"] = modes
    response["rates"] = rates
    response["filters"] = filters
    response["shapers"] = shapers
    response["matrix_profiles"] = matrix_profiles
    response["favorite_filters"] = _load_favorites()
    return response


# -- Config -------------------------------------------------------------------

@router.post("/config")
def set_config(req: ConfigRequest) -> Dict[str, Any]:
    """Apply any subset of the DSP knobs.

    Each non-None field triggers exactly one HQPlayer Set* command;
    the request is rejected as 503 on connection trouble. Successful
    fields are reported back so the client can reconcile partial
    failures (e.g. wrong index for the current mode)."""
    applied: Dict[str, Any] = {}
    failed: Dict[str, str] = {}
    try:
        with _hqp_lock:
            try:
                hqp = _get_hqp()
            except (BrokenPipeError, ConnectionError, OSError):
                _reset_hqp()
                hqp = _get_hqp()

            if req.mode is not None:
                if hqp.set_mode(req.mode):
                    applied["mode"] = req.mode
                else:
                    failed["mode"] = "set_mode rejected"

            if req.rate is not None:
                if hqp.set_rate(req.rate):
                    applied["rate"] = req.rate
                else:
                    failed["rate"] = "set_rate rejected"

            if req.filter is not None:
                if hqp.set_filter(req.filter, req.filter1x):
                    applied["filter"] = req.filter
                    if req.filter1x is not None:
                        applied["filter1x"] = req.filter1x
                else:
                    failed["filter"] = "set_filter rejected"

            if req.shaper is not None:
                if hqp.set_shaping(req.shaper):
                    applied["shaper"] = req.shaper
                else:
                    failed["shaper"] = "set_shaping rejected"

            if req.matrix_profile is not None:
                if hqp.matrix_set_profile(req.matrix_profile):
                    applied["matrix_profile"] = req.matrix_profile
                else:
                    failed["matrix_profile"] = "matrix_set_profile rejected"
    except (BrokenPipeError, ConnectionError, OSError) as e:
        raise HTTPException(status_code=503, detail=f"HQPlayer not reachable: {e}")

    return {"ok": not failed, "applied": applied, "failed": failed}


# -- Volume -------------------------------------------------------------------

@router.post("/volume")
def nudge_volume(req: VolumeRequest) -> Dict[str, Any]:
    """Step the master volume by ±N dB.

    HQPlayer's own VolumeUp/Down jumps in 0.5 dB increments, which is
    too granular for a tap-tap-tap UI. Read current volume from
    State, add the delta, write back via SetVolume; clamp into
    HQP's typical range so we don't slam the dB to silly values."""
    try:
        with _hqp_lock:
            try:
                hqp = _get_hqp()
            except (BrokenPipeError, ConnectionError, OSError):
                _reset_hqp()
                hqp = _get_hqp()
            state = hqp.get_state()
            if state is None:
                raise HTTPException(status_code=503, detail="HQPlayer state unavailable")
            current = float(state.get("volume", 0.0))
            new_vol = max(-120.0, min(0.0, current + float(req.delta)))
            ok = hqp.set_volume(new_vol)
    except (BrokenPipeError, ConnectionError, OSError) as e:
        raise HTTPException(status_code=503, detail=f"HQPlayer not reachable: {e}")
    if not ok:
        raise HTTPException(status_code=503, detail="set_volume rejected")
    return {"volume": new_vol}


# -- Favorites ----------------------------------------------------------------

@router.post("/favorites/filter")
def update_favorite_filter(req: FavoriteRequest) -> Dict[str, Any]:
    """Add or remove a filter name from the user's favourites list."""
    if req.action not in ("add", "remove"):
        raise HTTPException(status_code=400, detail="action must be 'add' or 'remove'")
    name = req.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="name is empty")
    favs = _load_favorites()
    if req.action == "add" and name not in favs:
        favs.append(name)
    elif req.action == "remove" and name in favs:
        favs.remove(name)
    _save_favorites(favs)
    return {"ok": True, "favorite_filters": favs}
