"""
HQPlayer settings endpoint.

Single round-trip /state for the More → HQPlayer screen: connection
status, current DSP selections, all available lists (modes / rates /
filters / shapers / matrix profiles) and the user's favourite-filter list
from the user_settings key/value store; with `dsp=1` (the screen itself)
also how each picker entry runs on this HQPlayer (`headroom`,
playback.hqp_load) and the benchmark's block (playback.hqp_benchmark).
Writes go through /config (any subset of filter / mode / rate / shaper /
matrix_profile), /favorites (add/remove a filter name from the favourites
list), /benchmark and /cuda.

The screen reads state on mount only — no polling. If the user
changes settings via HQP Desktop, a manual refresh in the UI picks
up the new values.
"""

import logging
import threading
import time
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from config import settings

logger = logging.getLogger(__name__)
from db_pool import db_execute, db_query_one
from hqplayer_client import VOLUME_FIXED
from playback.hqp_backend import (
    _hqp_lock,
    _hqp_status_lock,
    _get_hqp,
    _get_hqp_status,
    _reset_hqp,
    _reset_hqp_status,
)
from playback.manager import OutputHeld, manager

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


class BenchmarkRequest(BaseModel):
    mode: Optional[str] = None          # 'pcm' | 'sdm'; None = the mode HQPlayer is in
    fixed_volume_ok: bool = False       # the owner turned the amplifier down: HQPlayer cannot


class CudaRequest(BaseModel):
    value: Optional[str] = None         # 'off' | 'full' | 'convolution'; None = not known


class VolumeRequest(BaseModel):
    # +1 nudges up by 1 dB; -1 nudges down. Bigger steps are
    # accepted so the same endpoint can serve a larger button or a
    # slider drag. HQPlayer's own volume_up/down step is 0.5 dB,
    # which is too granular for tap-tap-tap; we apply set_volume
    # with the delta computed from the current state.
    delta: float


# -- State --------------------------------------------------------------------

@router.get("/state")
def get_state(dsp: bool = False) -> Dict[str, Any]:
    """Snapshot everything the HQPlayer settings screen needs. `dsp` adds
    what only that screen shows — the headroom marks, the Benchmark block
    and the volume range, a dozen queries and two more round-trips — and
    records what HQPlayer said of itself; the More drawer and the Output
    picker's dot ask the plain state, a connection check."""
    from playback.hqp_backend import _stream_mode, hqp_media_host
    response: Dict[str, Any] = {
        "host": settings.hqplayer_host,
        "port": settings.hqplayer_port,
        "connected": False,
        # How HQPlayer reaches the files — read off its address — and the
        # proxy address its media URLs name: the screen shows both so a
        # remote HQPlayer that cannot fetch anything is diagnosable from
        # the phone.
        "file_access": "stream" if _stream_mode() else "path",
        "media_url_host": hqp_media_host(),
        "media_url_port": settings.media_proxy_port,
    }
    # The row the Output picker registered for this HQPlayer — its name
    # is what the screen calls it, its id what Settings › Library keys on.
    import hqp_library
    ep = hqp_library.endpoint_by_address(settings.hqplayer_host, settings.hqplayer_port)
    response["endpoint_id"] = ep["id"] if ep else None
    response["name"] = ep["name"] if ep else None
    response["label"] = hqp_library.label(ep["name"], ep["product"]) if ep else None
    # Its library, when it has one of its own (another machine): what this
    # node holds of it and the running job — the screen's Library section.
    from routers.settings import _hqp_library_state
    lib = _hqp_library_state()
    response["library"] = {k: lib.get(k) for k in ("own_library", "synced", "files", "albums",
                                                   "measured", "running", "cancel_requested",
                                                   "progress", "last_synced_at")}
    if dsp:
        # The job and the last run whether or not HQPlayer answers now: a
        # heavy point can hold its control port past this read, and the
        # run's Cancel must still be there.
        from playback import hqp_benchmark
        response["benchmark"] = hqp_benchmark.summary(ep)

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
            status = hqp.get_status() if dsp else None
            volume_range = hqp.volume_range() if dsp else None
    except (BrokenPipeError, ConnectionError, OSError) as e:
        return {**response, "error": str(e)}

    # TCP connect() on its own isn't proof of being talking to the
    # HQPlayer control protocol — HQPlayer also listens on 4322 (its
    # metering stream, the control port + 1), on 8019 (the UPnP renderer)
    # and, Embedded, on 8088 (the web interface); and a stray service can
    # hold any port. Treat the connection as healthy only when GetInfo came
    # back with the actual `product` field — that's protocol-level
    # confirmation that the other side is HQPlayer.
    really_connected = bool(info and info.get("product"))
    response["connected"] = really_connected
    response["info"] = info or {}
    response["state"] = state or {}
    response["modes"] = modes or []
    response["rates"] = rates or []
    response["filters"] = filters or []
    response["shapers"] = shapers or []
    response["matrix_profiles"] = matrix_profiles
    response["favorite_filters"] = _load_favorites()
    if not dsp:
        return response
    if ep is not None and info:
        hqp_library.note_info(ep["id"], info, here=not _stream_mode())
        ep = hqp_library.endpoint_by_address(settings.hqplayer_host, settings.hqplayer_port)
    lists = {"modes": modes or [], "filters": filters or [], "shapers": shapers or [],
             "rates": rates or []}
    response["headroom"] = _headroom(ep, state, status, lists)
    response["benchmark"] = _benchmark_block(ep, state, lists, volume_range)
    response["volume_range"] = volume_range
    return response


def _mode_of(state: dict, modes: List[dict]) -> Optional[str]:
    """The mode a setting is measured in, by name: the one chosen, or for
    [source] the one HQPlayer runs (State active_mode is a ModesItem value)."""
    from playback.hqp_benchmark import mode_kind
    name = next((m["name"] for m in modes if m["index"] == state.get("mode")), None)
    if mode_kind(name) is None:
        name = next((m["name"] for m in modes if m["value"] == state.get("active_mode")), None)
    return name


def _headroom(ep: Optional[dict], state: Optional[dict], status, lists: dict) -> Dict[str, Any]:
    """How each picker entry runs on this HQPlayer, for the source the owner
    hears: the playing one (what HQPlayer decodes), else the file the queue
    plays next. Empty without a measured build or a known source."""
    from hqplayer_client import PlaybackState
    from playback import hqp_load
    from playback.hqp_benchmark import running_filter
    if not ep or not ep.get("hqp_engine") or not state:
        return {}
    if (status is not None and status.src_rate and status.src_channels
            and status.state in (PlaybackState.PLAYING, PlaybackState.PAUSED)):
        mode, rate_out = status.active_mode, status.active_rate
        filt, shaper = status.active_filter, status.active_shaper
        src = (status.src_rate, status.src_channels)
    else:
        item = manager.queue.item_at(manager.latest_status.get("track_index") or 1)
        src = hqp_load.file_source(item.opener()) if item is not None else None
        mode = _mode_of(state, lists["modes"])
        if src is None or mode is None:
            return {}
        hz = {r["index"]: r["rate"] for r in lists["rates"]}
        rate_out = hz.get(state["rate"]) or state.get("active_rate") or 0
        filt = running_filter({f["index"]: f["name"] for f in lists["filters"]},
                               state["filterNx"], state["filter1x"], src[0])
        shaper = next((sh["name"] for sh in lists["shapers"] if sh["index"] == state["shaper"]), "")
    ctx = hqp_load.context(ep, mode=mode, state=state)
    return hqp_load.headroom(ctx, rate_out=rate_out, filter=filt, shaper=shaper,
                             src_rate=src[0], src_channels=src[1])


def _benchmark_block(ep: Optional[dict], state: Optional[dict], lists: dict,
                     volume_range: Optional[dict]) -> Dict[str, Any]:
    """The Benchmark block: the job and the last run, CUDA offload and where
    it is known from, whether HQPlayer can lower its volume here, the
    machine it runs on (this one only), and what a run of the current mode
    would measure now."""
    import hqp_library
    from playback import hqp_benchmark
    from playback.hqp_backend import _stream_mode
    out = hqp_benchmark.summary(ep)
    here = not _stream_mode()
    out.update(cuda=(ep or {}).get("cuda"),
               cuda_read=here and hqp_library.local_cuda()[0],
               volume_control=bool(state) and hqp_benchmark.mute_level(
                   volume_range, state["volume"]) is not None,
               host=({k: ep.get(f"host_{k}") for k in ("cpu", "cores", "gpu", "ram_gb")}
                     if here and ep else None),
               estimate=None, other_modes=[])
    if not ep or not ep.get("hqp_engine") or not state or out["job"]["running"]:
        return out
    # A run measures the mode chosen in HQPlayer ([source] chooses none):
    # the others are measured on request.
    mode = next((m["name"] for m in lists["modes"] if m["index"] == state.get("mode")), None)
    kind = hqp_benchmark.mode_kind(mode)
    kinds = {hqp_benchmark.mode_kind(m["name"]) for m in lists["modes"]} - {None}
    out["other_modes"] = sorted(kinds - {kind})
    if kind is not None:
        grid = hqp_benchmark.plan(ep, kind=kind, mode_name=mode, state=state,
                                  filters=lists["filters"], shapers=lists["shapers"],
                                  rates=lists["rates"])
        asks = grid.asks()
        out["estimate"] = {"mode": mode, "points": asks,
                           "seconds": round(asks * hqp_benchmark.pace(ep["id"]))}
    return out


def _refuse_while_held() -> None:
    """A lent output (the benchmark drives HQPlayer) takes no DSP or volume
    change from here: it would land in the middle of a measurement. Called
    under _hqp_lock — the hold detaches the backend under it, so a change
    that passed the check is through before the run sends anything."""
    hold = manager.held
    if hold is not None:
        raise HTTPException(status_code=409, detail=str(OutputHeld(hold)))


# -- Config -------------------------------------------------------------------

# Applying a heavy DSP change mid-play (e.g. a long-initialisation filter
# like sinc-MGa) makes HQPlayer STOP the transport while it rebuilds the
# pipeline — and it never resumes on its own (light filters hot-swap with a
# sub-second dropout). The watcher below restores the exact pre-change spot
# once HQPlayer answers again. The 1 s wait-loop is the same documented
# boundary exception as the status poller: HQPlayer cannot push "I'm ready".
_RESUME_WATCH_DEADLINE_S = 90.0


def _resume_after_dsp_change(pre_index: int, pre_position: int, generation: int) -> None:
    deadline = time.monotonic() + _RESUME_WATCH_DEADLINE_S
    while time.monotonic() < deadline:
        time.sleep(1.0)
        if manager.queue.generation != generation:
            return                      # user started a new queue — back off
        state = manager.latest_status.get("state")
        if state in ("playing", "paused"):
            return                      # hot-swapped / user already resumed
        if state == "stopped":
            backend = manager.active
            if backend is None or backend.id != "hqplayer":
                return
            try:
                backend.resume_after_rebuild(pre_index, pre_position)
                logger.info("resumed playback after DSP rebuild "
                            "(track %d @ %ds)", pre_index, pre_position)
            except Exception as e:
                logger.warning("post-DSP resume failed: %s", e)
            return
        # "disconnected" — HQPlayer is still rebuilding; keep waiting.
    logger.warning("post-DSP resume: HQPlayer did not come back within %.0fs",
                   _RESUME_WATCH_DEADLINE_S)


@router.post("/config")
def set_config(req: ConfigRequest) -> Dict[str, Any]:
    """Apply any subset of the DSP knobs.

    Each non-None field triggers exactly one HQPlayer Set* command;
    the request is rejected as 503 on connection trouble. Successful
    fields are reported back so the client can reconcile partial
    failures (e.g. wrong index for the current mode). A change applied
    mid-play arms a resume watcher — see _resume_after_dsp_change."""
    pre = dict(manager.latest_status)
    try:
        with _hqp_lock:
            _refuse_while_held()
            try:
                hqp = _get_hqp()
            except (BrokenPipeError, ConnectionError, OSError):
                _reset_hqp()
                hqp = _get_hqp()
            applied, failed = hqp.apply_settings(
                mode=req.mode, rate=req.rate, filter=req.filter, filter1x=req.filter1x,
                shaper=req.shaper, matrix_profile=req.matrix_profile)
    except (BrokenPipeError, ConnectionError, OSError) as e:
        raise HTTPException(status_code=503, detail=f"HQPlayer not reachable: {e}")

    if applied and pre.get("state") == "playing":
        threading.Thread(
            target=_resume_after_dsp_change,
            args=(int(pre.get("track_index") or 1),
                  int(pre.get("position") or 0),
                  manager.queue.generation),
            daemon=True, name="hqp-dsp-resume").start()

    return {"ok": not failed, "applied": applied, "failed": failed}


# -- Volume -------------------------------------------------------------------

@router.post("/volume")
def nudge_volume(req: VolumeRequest) -> Dict[str, Any]:
    """Step the master volume by ±N dB within the range HQPlayer allows.

    HQPlayer's own VolumeUp/Down jumps in 0.5 dB increments, which is
    too granular for a tap-tap-tap UI. Read the current volume from
    State and the range from VolumeRange, write back via Volume. A
    volume HQPlayer holds fixed is refused here with the reason — its
    own answer to the Volume is a bare Error; with no range given, the
    step is HQPlayer's to judge."""
    try:
        with _hqp_lock:
            _refuse_while_held()
            try:
                hqp = _get_hqp()
            except (BrokenPipeError, ConnectionError, OSError):
                _reset_hqp()
                hqp = _get_hqp()
            state = hqp.get_state()
            if state is None:
                raise HTTPException(status_code=503, detail="HQPlayer state unavailable")
            span = hqp.volume_range()
            if span is not None and not span["enabled"]:
                raise HTTPException(status_code=409, detail=VOLUME_FIXED)
            new_vol = state["volume"] + req.delta
            if span is not None:
                new_vol = max(span["min"], min(span["max"], new_vol))
            ok = hqp.set_volume(new_vol)
            refusal = None if ok else hqp.refusal()
    except (BrokenPipeError, ConnectionError, OSError) as e:
        raise HTTPException(status_code=503, detail=f"HQPlayer not reachable: {e}")
    if not ok:
        raise HTTPException(status_code=503, detail=refusal)
    return {"volume": new_vol}


# -- Benchmark ----------------------------------------------------------------

@router.get("/benchmark")
def get_benchmark() -> Dict[str, Any]:
    """The job, the last run, and every key measured in the current context
    — the results sheet. When HQPlayer is another build than the last run
    measured, that run's results, marked stale."""
    import hqp_library
    from playback import hqp_benchmark, hqp_load
    ep = hqp_library.endpoint_by_address(settings.hqplayer_host, settings.hqplayer_port)
    out = hqp_benchmark.summary(ep)
    out.update(rows=[], context=None, current=None, thresholds=None)
    if ep is None:
        return out
    try:
        with _hqp_status_lock:
            try:
                hqp = _get_hqp_status()
            except (BrokenPipeError, ConnectionError, OSError):
                _reset_hqp_status()
                hqp = _get_hqp_status()
            state, modes = hqp.get_state(), hqp.get_modes()
            filters, shapers, rates = hqp.get_filters(), hqp.get_shapers(), hqp.get_rates()
    except (BrokenPipeError, ConnectionError, OSError) as e:
        raise HTTPException(status_code=503, detail=f"HQPlayer not reachable: {e}")
    if state is None:
        raise HTTPException(status_code=503, detail="HQPlayer state unavailable")
    mode = _mode_of(state, modes)
    run = out["last_run"]
    engine = run["hqp_engine"] if run and run["stale"] else ep.get("hqp_engine")
    ctx = hqp_load.context({**ep, "hqp_engine": engine}, mode=mode, state=state)
    from playback.hqp_benchmark import running_filter
    hz = {r["index"]: r["rate"] for r in rates}
    out.update(
        context={k: ctx[k] for k in ("engine", "cuda", "mode", "matrix_profile", "convolution")},
        current={"filter": running_filter({f["index"]: f["name"] for f in filters},
                                           state["filterNx"], state["filter1x"], 44100),
                 "shaper": next((s["name"] for s in shapers if s["index"] == state["shaper"]), ""),
                 "rate_out": hz.get(state["rate"]) or state.get("active_rate") or 0},
        thresholds=hqp_load.thresholds(ctx),
        rows=hqp_load.results(ctx))
    return out


@router.post("/benchmark")
def start_benchmark(req: BenchmarkRequest) -> Dict[str, Any]:
    """Start a run; 409 with the reason when it cannot start now."""
    from playback import hqp_benchmark
    if req.mode not in (None, "pcm", "sdm"):
        raise HTTPException(status_code=400, detail="mode must be 'pcm' or 'sdm'")
    try:
        return hqp_benchmark.start(req.mode, fixed_volume_ok=req.fixed_volume_ok)
    except hqp_benchmark.BenchmarkRefused as e:
        raise HTTPException(status_code=409, detail=str(e))


@router.post("/benchmark/cancel")
def cancel_benchmark() -> Dict[str, Any]:
    from playback import hqp_benchmark
    return {"cancelled": hqp_benchmark.cancel()}


@router.put("/cuda")
def put_cuda(req: CudaRequest) -> Dict[str, Any]:
    """The owner's answer about CUDA offload on an HQPlayer whose settings
    this node cannot read. One here is read from HQPlayer's own settings."""
    import hqp_library
    from playback.hqp_backend import _stream_mode
    if req.value not in (None, "off", "full", "convolution"):
        raise HTTPException(status_code=400, detail="value must be off, full or convolution")
    ep = hqp_library.endpoint_by_address(settings.hqplayer_host, settings.hqplayer_port)
    if ep is None:
        raise HTTPException(status_code=409, detail="This HQPlayer is not registered yet")
    if not _stream_mode() and hqp_library.local_cuda()[0]:
        raise HTTPException(status_code=409, detail="Read from HQPlayer's own settings on this computer")
    hqp_library.set_cuda(ep["id"], req.value)
    return {"cuda": req.value}


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
